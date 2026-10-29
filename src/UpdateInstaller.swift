// Native update handoff, using the staged-verification and atomic-exchange
// approach used by Stark. See docs/third-party/stark-MIT.md.
import AppKit
import Darwin
import Foundation
import Security

struct UpdateInstallError: LocalizedError {
    let message: String
    var backupPreserved = false
    var errorDescription: String? { message }
}

func installRequire(_ condition: Bool, _ message: String) throws {
    if !condition { throw UpdateInstallError(message: message) }
}

struct InstallVersion: Comparable, Equatable {
    let components: [Int]
    init(_ text: String, legacy: Bool = false) throws {
        let parts = text.split(separator: ".", omittingEmptySubsequences: false)
        try installRequire((legacy ? (1...3).contains(parts.count) : parts.count == 3), "The app version is invalid.")
        var values: [Int] = []
        for part in parts {
            try installRequire(!part.isEmpty && part.count <= 9 && part.utf8.allSatisfy { (48...57).contains($0) }
                && (part == "0" || !part.hasPrefix("0")), "The app version is invalid.")
            guard let value = Int(part) else { throw UpdateInstallError(message: "The app version is invalid.") }
            values.append(value)
        }
        self.components = values + Array(repeating: 0, count: 3 - values.count)
    }
    static func < (lhs: Self, rhs: Self) -> Bool { lhs.components.lexicographicallyPrecedes(rhs.components) }
}

struct InstallFileIdentity: Equatable {
    let device: dev_t
    let inode: ino_t
    let owner: uid_t
    init(_ path: URL, directory: Bool) throws {
        var info = stat()
        try installRequire(lstat(path.path, &info) == 0, "An update file is missing.")
        try installRequire((info.st_mode & S_IFMT) == (directory ? S_IFDIR : S_IFREG), "An update path has the wrong file type.")
        device = info.st_dev; inode = info.st_ino; owner = info.st_uid
    }
}

func installPhysicalPath(_ path: URL) throws -> URL {
    try installRequire(path.isFileURL && path.path.hasPrefix("/") && !path.path.unicodeScalars.contains { $0.value < 32 }, "An update path is invalid.")
    // Foundation's standardizedFileURL rewrites /private/var to /var on macOS,
    // introducing a symbolic link into a path that was already canonical.
    let clean = path
    var current = URL(fileURLWithPath: "/", isDirectory: true)
    for part in clean.pathComponents.dropFirst() {
        current.appendPathComponent(part)
        var info = stat()
        try installRequire(lstat(current.path, &info) == 0 && (info.st_mode & S_IFMT) != S_IFLNK, "An update path contains a symbolic link or is missing.")
    }
    guard let resolved = realpath(clean.path, nil) else { throw UpdateInstallError(message: "An update path cannot be resolved.") }
    defer { free(resolved) }
    let result = URL(fileURLWithPath: String(cString: resolved), isDirectory: path.hasDirectoryPath)
    try installRequire(result.path == clean.path, "An update path is not canonical.")
    return result
}

func installValidateWorkspace(_ workspace: URL) throws {
    _ = try installPhysicalPath(workspace)
    var info = stat()
    try installRequire(lstat(workspace.path, &info) == 0 && (info.st_mode & S_IFMT) == S_IFDIR
        && info.st_uid == getuid() && (info.st_mode & 0o777) == 0o700, "The private update workspace is invalid.")
    guard let resolvedTemp = realpath(FileManager.default.temporaryDirectory.path, nil) else {
        throw UpdateInstallError(message: "The temporary folder cannot be resolved.")
    }
    defer { free(resolvedTemp) }
    let temp = String(cString: resolvedTemp) + "/"
    try installRequire(workspace.path.hasPrefix(temp) && workspace.path != String(temp.dropLast()), "The update workspace is outside the temporary folder.")
}

func installValidateTargetPath(_ target: URL) throws {
    _ = try installPhysicalPath(target)
    let homeApps = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications/Diskie.app").path
    try installRequire(["/Applications/Diskie.app", "/Applications/Disk Space Explorer.app", homeApps].contains(target.path),
        "Install Diskie in Applications before updating it.")
    let identity = try InstallFileIdentity(target, directory: true)
    try installRequire(identity.owner == getuid(), "This copy of Diskie belongs to another user. Install a copy you own before updating.")
    let parent = target.deletingLastPathComponent()
    var info = stat()
    try installRequire(lstat(parent.path, &info) == 0 && (info.st_mode & S_IFMT) == S_IFDIR
        && (info.st_uid == 0 || info.st_uid == getuid()) && (info.st_mode & 0o002) == 0
        && access(parent.path, W_OK) == 0, "The Applications folder is not writable by this user.")
}

struct InstallBundle {
    let version: InstallVersion
    let executable: String
    let digest: Data
    let identity: InstallFileIdentity
}

func installValidateBundle(_ app: URL, expectedVersion: String? = nil, gatekeeper: Bool = true) throws -> InstallBundle {
    let identity = try InstallFileIdentity(app, directory: true)
    try installRequire(identity.owner == getuid(), "An update app belongs to another user.")
    let metadataURL = app.appendingPathComponent("Contents/Info.plist")
    _ = try installPhysicalPath(metadataURL)
    guard let info = try PropertyListSerialization.propertyList(from: Data(contentsOf: metadataURL), format: nil) as? [String: Any],
          let versionText = info["CFBundleShortVersionString"] as? String,
          let executable = info["CFBundleExecutable"] as? String else { throw UpdateInstallError(message: "The app metadata is invalid.") }
    try installRequire(info["CFBundleIdentifier"] as? String == "local.diskspace.explorer"
        && !executable.isEmpty && !executable.contains("/") && !executable.contains("\\") && executable != "." && executable != "..",
        "The app identity is invalid.")
    let version = try InstallVersion(versionText, legacy: expectedVersion == nil)
    if let expectedVersion = expectedVersion {
        try installRequire(versionText == expectedVersion && executable == "Diskie", "The downloaded app does not match the selected release.")
    }
    _ = try installPhysicalPath(app.appendingPathComponent("Contents/MacOS/" + executable))
    var code: SecStaticCode?
    try installRequire(SecStaticCodeCreateWithPath(app as CFURL, [], &code) == errSecSuccess && code != nil, "The app signature cannot be read.")
    let requirementText = "anchor apple generic and identifier \"local.diskspace.explorer\" and certificate 1[field.1.2.840.113635.100.6.2.6] exists and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = \"S5BT2PRN5B\""
    var requirement: SecRequirement?
    try installRequire(SecRequirementCreateWithString(requirementText as CFString, [], &requirement) == errSecSuccess, "The update trust policy is invalid.")
    let flags = SecCSFlags(rawValue: kSecCSStrictValidate | kSecCSCheckAllArchitectures | kSecCSCheckNestedCode)
    try installRequire(SecStaticCodeCheckValidity(code!, flags, requirement) == errSecSuccess,
        "The app signature is invalid or does not match Diskie’s developer.")
    var signingInfo: CFDictionary?
    try installRequire(SecCodeCopySigningInformation(code!, SecCSFlags(rawValue: kSecCSSigningInformation), &signingInfo) == errSecSuccess,
        "The app signing information is unavailable.")
    guard let signing = signingInfo as? [String: Any], let digest = signing[kSecCodeInfoUnique as String] as? Data,
          let signingFlags = signing[kSecCodeInfoFlags as String] as? NSNumber else { throw UpdateInstallError(message: "The app signing information is incomplete.") }
    if expectedVersion != nil {
        try installRequire((signingFlags.uint32Value & 0x10000) != 0, "The app is missing its hardened runtime signature.")
    }
    if gatekeeper { try installCommand("/usr/sbin/spctl", ["--assess", "--type", "execute", app.path], "macOS did not approve the downloaded app.") }
    try installRequire(try InstallFileIdentity(app, directory: true) == identity, "The app changed while it was being verified.")
    return InstallBundle(version: version, executable: executable, digest: digest, identity: identity)
}

func installCommand(_ executable: String, _ arguments: [String], _ failure: String, timeout: TimeInterval = 60) throws {
    let task = Process(); task.executableURL = URL(fileURLWithPath: executable); task.arguments = arguments
    task.standardOutput = FileHandle.nullDevice; task.standardError = FileHandle.nullDevice
    task.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": FileManager.default.homeDirectoryForCurrentUser.path,
                        "TMPDIR": FileManager.default.temporaryDirectory.path, "LANG": "en_US.UTF-8"]
    do { try task.run() } catch { throw UpdateInstallError(message: failure) }
    let deadline = Date().addingTimeInterval(timeout)
    while task.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
    if task.isRunning {
        // Only this helper's own child is signalled, never an app process.
        task.terminate()
        let grace = Date().addingTimeInterval(2)
        while task.isRunning && Date() < grace { Thread.sleep(forTimeInterval: 0.05) }
        if task.isRunning { _ = kill(task.processIdentifier, SIGKILL) }
        throw UpdateInstallError(message: failure + " The command timed out.")
    }
    task.waitUntilExit()
    try installRequire(task.terminationReason == .exit && task.terminationStatus == 0, failure)
}

struct InstallProcessIdentity: Equatable {
    let pid: pid_t
    let seconds: UInt64
    let microseconds: UInt64
    let uid: uid_t
    let path: String
    init?(_ pid: pid_t) {
        var info = proc_bsdinfo()
        guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, Int32(MemoryLayout<proc_bsdinfo>.stride)) == MemoryLayout<proc_bsdinfo>.stride else { return nil }
        var pathBytes = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
        guard proc_pidpath(pid, &pathBytes, UInt32(pathBytes.count)) > 0 else { return nil }
        self.pid = pid; seconds = info.pbi_start_tvsec; microseconds = info.pbi_start_tvusec; uid = info.pbi_uid
        path = String(cString: pathBytes)
    }
}

func installProcesses(at executable: String,
                      listPIDs: (UnsafeMutableRawPointer?, Int32) -> Int32 = { proc_listallpids($0, $1) }) throws -> [InstallProcessIdentity] {
    for attempt in 0..<4 {
        let count = listPIDs(nil, 0)
        try installRequire(count > 0 && count < 1_000_000, "Running applications could not be inspected. The update was not installed.")
        let capacity = Int(count) + (128 << attempt)
        var pids = [pid_t](repeating: 0, count: capacity)
        let actual = pids.withUnsafeMutableBytes { listPIDs($0.baseAddress, Int32($0.count)) }
        try installRequire(actual > 0, "Running applications could not be inspected. The update was not installed.")
        // libproc does not distinguish a full buffer from a truncated inventory.
        // Retry with headroom; an incomplete inventory never means no apps.
        if actual >= capacity { continue }
        return pids.prefix(Int(actual)).compactMap { InstallProcessIdentity($0) }.filter { $0.path == executable }
    }
    throw UpdateInstallError(message: "Running applications changed too quickly to inspect. The update was not installed.")
}

func installRequireNoProcesses(at executables: [String]) throws {
    for executable in Set(executables) {
        try installRequire(try installProcesses(at: executable).isEmpty,
            "Diskie was reopened while the update was being prepared. Quit it and retry the update.")
    }
}

func installWrite(_ object: [String: Any], to url: URL) throws {
    let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    try data.write(to: url, options: .atomic)
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
}

final class InstallTargetLock {
    private let descriptor: Int32
    private let path: URL
    private let identity: InstallFileIdentity
    init(parent: URL, targetName: String) throws {
        path = parent.appendingPathComponent("." + targetName + ".update-lock")
        descriptor = Darwin.open(path.path, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, mode_t(0o600))
        try installRequire(descriptor >= 0, "The update lock could not be opened.")
        var info = stat()
        guard fstat(descriptor, &info) == 0 && info.st_uid == getuid() && (info.st_mode & S_IFMT) == S_IFREG
                && (info.st_mode & 0o777) == 0o600 && info.st_nlink == 1 else {
            Darwin.close(descriptor); throw UpdateInstallError(message: "The update lock is invalid.")
        }
        guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
            Darwin.close(descriptor); throw UpdateInstallError(message: "Another Diskie update is in progress.")
        }
        identity = try InstallFileIdentity(path, directory: false)
    }
    deinit {
        if let current = try? InstallFileIdentity(path, directory: false), current == identity { _ = unlink(path.path) }
        _ = flock(descriptor, LOCK_UN); Darwin.close(descriptor)
    }
}

func installExchange(_ first: URL, _ second: URL) throws {
    try installRequire(renamex_np(first.path, second.path, UInt32(RENAME_SWAP)) == 0, "macOS could not replace Diskie. The original installation was retained.")
}

// Keep activation and recovery together so the same exchange/rollback sequence
// can be exercised with temporary directories in tests.
func installActivate(staged: URL, target: URL, beforeExchange: () throws -> Void = {}, launch: () throws -> Void,
                     verifyRecovery: () throws -> Void, stopFailedLaunch: () throws -> Void) throws {
    try beforeExchange()
    try installExchange(staged, target)
    do { try launch() }
    catch {
        let launchError = error
        do {
            try verifyRecovery()
            try stopFailedLaunch()
            try installExchange(staged, target)
        } catch {
            throw UpdateInstallError(message: "Diskie could not finish updating or restore the old version. The previous app remains at \(staged.path).", backupPreserved: true)
        }
        throw launchError
    }
}

func installWaitForExit(_ parent: InstallProcessIdentity, executable: String, timeout: TimeInterval = 45) throws {
    let deadline = Date().addingTimeInterval(timeout)
    while true {
        let running = try installProcesses(at: executable)
        if InstallProcessIdentity(parent.pid) != parent && running.isEmpty { return }
        try installRequire(Date() < deadline, "Diskie did not quit in time. The update was not installed.")
        Thread.sleep(forTimeInterval: 0.1)
    }
}

func installLaunch(_ target: URL, executable: String) throws {
    try installCommand("/usr/bin/open", ["-n", target.path], "The updated app could not be opened.")
    let deadline = Date().addingTimeInterval(20)
    var firstSeen: Date?
    while Date() < deadline {
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: "local.diskspace.explorer").filter {
            $0.bundleURL?.standardizedFileURL.path == target.path && $0.executableURL?.standardizedFileURL.path == executable
                && !$0.isTerminated && $0.isFinishedLaunching
        }
        if running.count == 1, let process = InstallProcessIdentity(running[0].processIdentifier), process.uid == getuid(), process.path == executable {
            if firstSeen == nil { firstSeen = Date() }
            if Date().timeIntervalSince(firstSeen!) >= 2 { return }
        } else { firstSeen = nil }
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
    }
    throw UpdateInstallError(message: "The updated app did not finish launching. The previous installation will be restored.")
}

func installStopFailedLaunch(_ target: URL, executable: String) throws {
    let applications = NSRunningApplication.runningApplications(withBundleIdentifier: "local.diskspace.explorer").filter {
        $0.bundleURL?.standardizedFileURL.path == target.path && $0.executableURL?.standardizedFileURL.path == executable
    }
    for application in applications {
        guard let identity = InstallProcessIdentity(application.processIdentifier), identity.uid == getuid(), identity.path == executable else {
            throw UpdateInstallError(message: "The updated app could not be stopped. Its previous version was kept in the backup folder.")
        }
        _ = application.terminate()
    }
    let deadline = Date().addingTimeInterval(10)
    while !(try installProcesses(at: executable)).isEmpty {
        try installRequire(Date() < deadline, "The updated app could not be stopped. Its previous version was kept in the backup folder.")
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
    }
}

struct InstallOptions {
    let workspace: URL
    let target: URL
    let parentPID: pid_t
    let version: String
    init(arguments: [String]) throws {
        try installRequire(arguments.count == 8, "The update helper arguments are invalid.")
        var values: [String: String] = [:]
        for index in stride(from: 0, to: arguments.count, by: 2) {
            let key = arguments[index]
            try installRequire(["--workspace", "--target", "--parent-pid", "--version"].contains(key) && values[key] == nil,
                "The update helper arguments are invalid.")
            values[key] = arguments[index + 1]
        }
        guard let workspace = values["--workspace"], let target = values["--target"], let pidText = values["--parent-pid"],
              let pid = pid_t(pidText), pid > 1, let version = values["--version"] else { throw UpdateInstallError(message: "The update helper arguments are invalid.") }
        _ = try InstallVersion(version)
        self.workspace = URL(fileURLWithPath: workspace, isDirectory: true)
        self.target = URL(fileURLWithPath: target, isDirectory: true)
        self.parentPID = pid; self.version = version
    }
}

func performInstall(_ options: InstallOptions) throws {
    let fm = FileManager.default
    let workspace = options.workspace, target = options.target
    try installValidateWorkspace(workspace); try installValidateTargetPath(target)
    try installRequire(!fm.fileExists(atPath: workspace.appendingPathComponent("ready").path)
        && !fm.fileExists(atPath: workspace.appendingPathComponent("result.json").path), "This update workspace has already been used.")
    let parentDirectory = target.deletingLastPathComponent()
    let lock = try InstallTargetLock(parent: parentDirectory, targetName: target.lastPathComponent)
    defer { withExtendedLifetime(lock) {} }
    let original = try installValidateBundle(target, gatekeeper: false)
    let originalExecutable = target.appendingPathComponent("Contents/MacOS/" + original.executable).path
    guard let parent = InstallProcessIdentity(options.parentPID), parent.uid == getuid(), parent.path == originalExecutable,
          parent.pid == getppid() else { throw UpdateInstallError(message: "The running Diskie process does not match this installation.") }
    try installRequire(try installProcesses(at: originalExecutable) == [parent], "Another copy of this installation is running. Quit it and retry the update.")
    let candidate = workspace.appendingPathComponent("candidate.app", isDirectory: true)
    _ = try installPhysicalPath(candidate)
    let desired = try installValidateBundle(candidate, expectedVersion: options.version)
    try installRequire(desired.version > original.version, "This release is not newer than the installed app.")
    let stageDirectory = parentDirectory.appendingPathComponent(".diskie-update-" + UUID().uuidString, isDirectory: true)
    try fm.createDirectory(at: stageDirectory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
    let staged = stageDirectory.appendingPathComponent("Diskie.app", isDirectory: true)
    var preserveBackup = false
    defer { if !preserveBackup { try? fm.removeItem(at: stageDirectory) } }
    try fm.copyItem(at: candidate, to: staged)
    let verified = try installValidateBundle(staged, expectedVersion: options.version)
    try installRequire(verified.digest == desired.digest, "The staged app does not match the downloaded app.")
    let current = try installValidateBundle(target, gatekeeper: false)
    try installRequire(current.identity == original.identity && current.digest == original.digest
        && InstallProcessIdentity(parent.pid) == parent && (try installProcesses(at: originalExecutable)) == [parent],
        "The installed app changed while preparing the update.")
    try installValidateWorkspace(workspace)
    try installWrite(["stage": "ready"], to: workspace.appendingPathComponent("ready"))
    try installWaitForExit(parent, executable: originalExecutable)
    try installValidateTargetPath(target)
    let beforeSwap = try installValidateBundle(target, gatekeeper: false)
    let beforeSwapStaged = try installValidateBundle(staged, expectedVersion: options.version)
    try installRequire(beforeSwap.identity == original.identity && beforeSwap.digest == original.digest
        && beforeSwapStaged.identity == verified.identity && beforeSwapStaged.digest == desired.digest,
        "An app changed before replacement. The update was not installed.")
    do {
        try installActivate(staged: staged, target: target, beforeExchange: {
            // Signature and Gatekeeper checks above may take time. Recheck the
            // physical paths and live process inventory immediately before the
            // exchange, so reopening the old app during verification aborts.
            try installValidateTargetPath(target)
            try installRequireNoProcesses(at: [originalExecutable, target.appendingPathComponent("Contents/MacOS/Diskie").path])
            let finalTarget = try InstallFileIdentity(target, directory: true)
            let finalStaged = try InstallFileIdentity(staged, directory: true)
            try installRequire(finalTarget == original.identity && finalStaged == verified.identity,
                "An app changed before replacement. The update was not installed.")
        }, launch: {
            try installLaunch(target, executable: target.appendingPathComponent("Contents/MacOS/Diskie").path)
        }, verifyRecovery: {
            let active = try installValidateBundle(target, expectedVersion: options.version, gatekeeper: false)
            let backup = try installValidateBundle(staged, gatekeeper: false)
            try installRequire(active.digest == desired.digest && backup.identity == original.identity && backup.digest == original.digest,
                "The app changed during recovery. The previous version was kept in the backup folder.")
        }, stopFailedLaunch: {
            try installStopFailedLaunch(target, executable: target.appendingPathComponent("Contents/MacOS/Diskie").path)
        })
        preserveBackup = true
        try installWrite(["success": true, "message": "Diskie was updated to \(options.version).", "backupPath": staged.path],
            to: workspace.appendingPathComponent("result.json"))
    } catch {
        if (error as? UpdateInstallError)?.backupPreserved == true { preserveBackup = true }
        throw error
    }
}

func updateInstallerMain(_ arguments: [String]) -> Int32 {
    var options: InstallOptions?
    do {
        let parsed = try InstallOptions(arguments: arguments); options = parsed
        try performInstall(parsed)
        return 0
    } catch {
        let message = (error as? UpdateInstallError)?.message ?? "Diskie could not complete the update. Your app data was not changed."
        if let options = options, (try? installValidateWorkspace(options.workspace)) != nil {
            try? installWrite(["success": false, "message": message], to: options.workspace.appendingPathComponent("result.json"))
            if FileManager.default.fileExists(atPath: options.workspace.appendingPathComponent("ready").path),
               (try? installValidateTargetPath(options.target)) != nil,
               let original = try? installValidateBundle(options.target, gatekeeper: false),
               (try? installProcesses(at: options.target.appendingPathComponent("Contents/MacOS/" + original.executable).path).isEmpty) == true {
                try? installCommand("/usr/bin/open", ["-n", options.target.path], "Diskie could not be reopened.")
            }
        }
        FileHandle.standardError.write(Data((message + "\n").utf8))
        return 1
    }
}

#if !DISKIE_UPDATER_TESTING
@main
struct DiskieUpdateInstaller {
    static func main() { exit(updateInstallerMain(Array(CommandLine.arguments.dropFirst()))) }
}
#endif
