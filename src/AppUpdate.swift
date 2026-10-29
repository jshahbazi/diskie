// Release validation and preparation adapted from Stark's AppUpdate.swift.
// See docs/third-party/stark-MIT.md.
import Cocoa
import CryptoKit
import Darwin

struct DiskieVersion: Comparable {
    let parts: [Int]
    init?(_ text: String, installed: Bool = false) {
        let fields = text.split(separator: ".", omittingEmptySubsequences: false)
        guard (installed ? (1...3).contains(fields.count) : fields.count == 3),
              fields.allSatisfy({ !$0.isEmpty && $0.count <= 9 && ($0 == "0" || $0.first != "0") && $0.utf8.allSatisfy({ (48...57).contains($0) }) }) else { return nil }
        parts = fields.compactMap { Int($0) } + Array(repeating: 0, count: 3 - fields.count)
    }
    static func < (lhs: Self, rhs: Self) -> Bool { lhs.parts.lexicographicallyPrecedes(rhs.parts) }
}

enum DiskieUpdateError: LocalizedError {
    case release, download, checksum, signature, metadata, location, command, helper(String)
    var errorDescription: String? {
        switch self {
        case .release: return "The latest GitHub release does not contain a compatible, verified Diskie installer."
        case .download: return "Could not download the update. Check your connection and try again."
        case .checksum: return "The update's size or SHA-256 checksum did not match the release."
        case .signature: return "The update could not be verified as a notarized Diskie release from its developer."
        case .metadata: return "The downloaded app does not match this release or is incompatible with this Mac."
        case .location: return "Install a signed Diskie release in a writable Applications folder before updating."
        case .command: return "macOS could not prepare the update. Your current app was kept."
        case .helper(let message): return message
        }
    }
}

struct DiskieRelease: Decodable {
    struct Asset: Decodable { let name: String; let browser_download_url: String; let digest: String?; let size: Int64 }
    let tag_name: String
    let draft: Bool
    let prerelease: Bool
    let assets: [Asset]
    func update(after current: String) throws -> DiskieDownload? {
        guard !draft, !prerelease, tag_name.first == "v",
              let version = DiskieVersion(String(tag_name.dropFirst())),
              let installed = DiskieVersion(current, installed: true) else { throw DiskieUpdateError.release }
        guard version > installed else { return nil }
        #if !arch(arm64)
        throw DiskieUpdateError.release
        #else
        let number = String(tag_name.dropFirst())
        let name = "diskie-\(number)-aarch64-apple-darwin.dmg"
        let address = "https://github.com/jshahbazi/diskie/releases/download/\(tag_name)/\(name)"
        let matches = assets.filter { $0.name == name }
        guard matches.count == 1, let asset = matches.first, asset.browser_download_url == address,
              let url = URL(string: address), let digest = asset.digest, digest.hasPrefix("sha256:"),
              digest.count == 71, digest.dropFirst(7).utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }),
              asset.size > 0, asset.size <= 268_435_456 else { throw DiskieUpdateError.release }
        return DiskieDownload(version: number, url: url, sha256: String(digest.dropFirst(7)), size: asset.size)
        #endif
    }
}

struct DiskieDownload { let version: String; let url: URL; let sha256: String; let size: Int64 }

// Only GitHub's HTTPS release delivery hosts may receive redirects. No tokens
// or local file paths are sent; scans and snapshots never enter this session.
final class DiskieRedirects: NSObject, URLSessionTaskDelegate {
    static func allowed(_ url: URL?) -> Bool {
        guard let url = url, url.scheme == "https", url.user == nil, url.password == nil,
              url.port == nil || url.port == 443, let host = url.host else { return false }
        return ["github.com", "api.github.com"].contains(host) || host.hasSuffix(".githubusercontent.com")
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(Self.allowed(request.url) ? request : nil)
    }
}

enum DiskieUpdateFiles {
    static let team = "S5BT2PRN5B"
    static let appID = "local.diskspace.explorer"
    static let manager = FileManager.default
    static func workspace() throws -> URL {
        let url = manager.temporaryDirectory.appendingPathComponent("diskie-update-" + UUID().uuidString, isDirectory: true)
        try manager.createDirectory(at: url, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        guard let physical = realpath(url.path, nil) else { try? manager.removeItem(at: url); throw DiskieUpdateError.command }
        defer { free(physical) }
        return URL(fileURLWithPath: String(cString: physical), isDirectory: true)
    }
    static func isWorkspace(_ url: URL) -> Bool {
        var info = stat()
        return url.deletingLastPathComponent().resolvingSymlinksInPath().path == manager.temporaryDirectory.resolvingSymlinksInPath().path &&
            url.lastPathComponent.hasPrefix("diskie-update-") && lstat(url.path, &info) == 0 &&
            info.st_mode & S_IFMT == S_IFDIR && info.st_uid == getuid() && info.st_mode & 0o777 == 0o700
    }
    static func command(_ executable: String, _ arguments: [String], timeout: TimeInterval = 90) throws {
        let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = arguments
        process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": NSHomeDirectory(), "TMPDIR": manager.temporaryDirectory.path]
        process.standardInput = FileHandle.nullDevice; process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        try process.run()
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
        if process.isRunning {
            process.terminate(); Thread.sleep(forTimeInterval: 0.2)
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            process.waitUntilExit(); throw DiskieUpdateError.command
        }
        guard process.terminationStatus == 0 else { throw DiskieUpdateError.command }
    }
    static func signature(_ url: URL, app: Bool, notarized: Bool = true) throws {
        let identifier = app ? " and identifier \"\(appID)\"" : ""
        let requirement = "=anchor apple generic\(identifier) and certificate 1[field.1.2.840.113635.100.6.2.6] exists and " +
            "certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = \"\(team)\""
        do {
            try command("/usr/bin/codesign", ["--verify", "--deep", "--strict", "-R", requirement, url.path])
            if notarized && app { try command("/usr/sbin/spctl", ["--assess", "--type", "execute", url.path]) }
        } catch { throw DiskieUpdateError.signature }
    }
    static func metadata(_ app: URL, version: String) throws {
        guard let data = try? Data(contentsOf: app.appendingPathComponent("Contents/Info.plist")),
              let info = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
              info["CFBundleIdentifier"] as? String == appID,
              info["CFBundleExecutable"] as? String == "Diskie",
              info["CFBundleName"] as? String == "Diskie",
              info["CFBundleShortVersionString"] as? String == version,
              let minimum = info["LSMinimumSystemVersion"] as? String,
              let required = DiskieVersion(minimum, installed: true),
              ProcessInfo.processInfo.isOperatingSystemAtLeast(OperatingSystemVersion(majorVersion: required.parts[0], minorVersion: required.parts[1], patchVersion: required.parts[2])) else { throw DiskieUpdateError.metadata }
        let executable = app.appendingPathComponent("Contents/MacOS/Diskie")
        let handle = try FileHandle(forReadingFrom: executable); defer { try? handle.close() }
        guard let header = try handle.read(upToCount: 4096), supportsArm64(header) else { throw DiskieUpdateError.metadata }
    }
    static func supportsArm64(_ data: Data) -> Bool {
        let bytes = [UInt8](data); guard bytes.count >= 8 else { return false }
        func word(_ offset: Int, little: Bool) -> UInt32 {
            let four = Array(bytes[offset..<offset + 4])
            return (little ? Array(four.reversed()) : four).reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        }
        let magic = Array(bytes.prefix(4))
        if magic == [0xcf, 0xfa, 0xed, 0xfe] { return word(4, little: true) == 0x0100000c }
        if magic == [0xfe, 0xed, 0xfa, 0xcf] { return word(4, little: false) == 0x0100000c }
        let fat32 = magic == [0xca, 0xfe, 0xba, 0xbe] || magic == [0xbe, 0xba, 0xfe, 0xca]
        let fat64 = magic == [0xca, 0xfe, 0xba, 0xbf] || magic == [0xbf, 0xba, 0xfe, 0xca]
        guard fat32 || fat64 else { return false }
        let little = magic.first == 0xbe || magic.first == 0xbf
        let count = Int(word(4, little: little)), stride = fat64 ? 32 : 20
        guard count > 0, count <= 64, bytes.count >= 8 + count * stride else { return false }
        return (0..<count).contains { word(8 + $0 * stride, little: little) == 0x0100000c }
    }
    static func verifyDigest(_ image: URL, release: DiskieDownload) throws {
        let handle = try FileHandle(forReadingFrom: image); defer { try? handle.close() }
        var hash = SHA256(); var size: Int64 = 0
        while let bytes = try handle.read(upToCount: 1024 * 1024), !bytes.isEmpty {
            hash.update(data: bytes); size += Int64(bytes.count)
        }
        guard size == release.size, hash.finalize().map({ String(format: "%02x", $0) }).joined() == release.sha256 else { throw DiskieUpdateError.checksum }
    }
    static func prepare(image: URL, release: DiskieDownload, workspace: URL) throws {
        try verifyDigest(image, release: release)
        try signature(image, app: false)
        try command("/usr/sbin/spctl", ["--assess", "--type", "open", "--context", "context:primary-signature", image.path])
        try command("/usr/bin/hdiutil", ["verify", image.path])
        let mount = workspace.appendingPathComponent("mount", isDirectory: true)
        try manager.createDirectory(at: mount, withIntermediateDirectories: false)
        var attached = false
        defer {
            if attached {
                do { try command("/usr/bin/hdiutil", ["detach", mount.path]) }
                catch { try? command("/usr/bin/hdiutil", ["detach", "-force", mount.path]) }
            }
        }
        // The explicit mountpoint lets cleanup find a mount even on a partial attach failure.
        attached = true
        try command("/usr/bin/hdiutil", ["attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", mount.path, image.path])
        let source = mount.appendingPathComponent("Diskie.app", isDirectory: true)
        let candidate = workspace.appendingPathComponent("candidate.app", isDirectory: true)
        try signature(source, app: true); try metadata(source, version: release.version)
        try manager.copyItem(at: source, to: candidate)
        try signature(candidate, app: true); try metadata(candidate, version: release.version)
    }
}

final class DiskieUpdater {
    var onChange: (() -> Void)?
    var onReadyToRestart: (() -> Void)?
    private(set) var isBusy = false
    private var checking = false
    private var available: DiskieDownload?
    private var phase: String?
    private weak var window: NSWindow?
    private var timer: Timer?
    private var helper: Process?
    private let pendingKey = "DiskiePendingUpdateWorkspace"
    private let current = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0"
    private let session: URLSession
    var buttonTitle: String { phase ?? (checking ? "Checking for Updates…" : available.map { "Update to \($0.version)…" } ?? "Check for Updates…") }
    init(window: NSWindow) {
        self.window = window
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 30; config.timeoutIntervalForResource = 300; config.urlCache = nil
        session = URLSession(configuration: config, delegate: DiskieRedirects(), delegateQueue: nil)
        timer = Timer.scheduledTimer(withTimeInterval: 6 * 3600, repeats: true) { [weak self] _ in self?.checkForUpdates() }
        recoverPendingUpdate()
    }
    deinit { timer?.invalidate(); session.invalidateAndCancel() }
    private func message(_ text: String, detail: String) {
        let alert = NSAlert(); alert.messageText = text; alert.informativeText = detail
        if let window = window { alert.beginSheetModal(for: window) } else { alert.runModal() }
    }
    func checkForUpdates(manual: Bool = false) {
        guard !checking, !isBusy else { return }
        checking = true; onChange?()
        Task { @MainActor in
            defer { checking = false; onChange?() }
            do {
                var request = URLRequest(url: URL(string: "https://api.github.com/repos/jshahbazi/diskie/releases/latest")!)
                request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
                request.setValue("Diskie/\(current)", forHTTPHeaderField: "User-Agent")
                let (data, response) = try await session.data(for: request)
                guard (response as? HTTPURLResponse)?.statusCode == 200, data.count <= 2_097_152,
                      DiskieRedirects.allowed(response.url) else { throw DiskieUpdateError.download }
                available = try JSONDecoder().decode(DiskieRelease.self, from: data).update(after: current)
                if manual {
                    if let release = available { message("Diskie \(release.version) is available", detail: "Use Update to \(release.version) in the toolbar to install it and restart Diskie.") }
                    else { message("Diskie is up to date", detail: "You’re running Diskie \(current).") }
                }
            } catch {
                if manual { message("Could not check for updates", detail: error.localizedDescription) }
            }
        }
    }
    func performAction() {
        guard !isBusy, !checking else { return }
        guard let release = available else { checkForUpdates(manual: true); return }
        let alert = NSAlert(); alert.messageText = "Install Diskie \(release.version)?"
        alert.informativeText = "Diskie will download and verify the release from GitHub, then restart. Any running scan will stop; your saved scan will remain available."
        alert.addButton(withTitle: "Install and Restart"); alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window!) { [weak self] response in
            if response == .alertFirstButtonReturn { self?.install(release) }
        }
    }
    private func install(_ release: DiskieDownload) {
        isBusy = true; phase = "Downloading update…"; onChange?()
        Task { @MainActor in
            var workspace: URL?
            var handedOff = false
            defer {
                if !handedOff {
                    if let workspace = workspace { try? FileManager.default.removeItem(at: workspace) }
                    isBusy = false; phase = nil; onChange?()
                }
            }
            do {
                let target = Bundle.main.bundleURL.standardizedFileURL
                let parent = target.deletingLastPathComponent().path
                guard ["/Applications", NSHomeDirectory() + "/Applications"].contains(parent),
                      ["Diskie.app", "Disk Space Explorer.app"].contains(target.lastPathComponent),
                      FileManager.default.isWritableFile(atPath: parent) else { throw DiskieUpdateError.location }
                try await Task.detached { try DiskieUpdateFiles.signature(target, app: true, notarized: false) }.value
                let space = try DiskieUpdateFiles.workspace(); workspace = space
                let (temporary, response) = try await session.download(from: release.url)
                guard (response as? HTTPURLResponse)?.statusCode == 200, DiskieRedirects.allowed(response.url) else {
                    try? FileManager.default.removeItem(at: temporary); throw DiskieUpdateError.download
                }
                let image = space.appendingPathComponent("release.dmg")
                try FileManager.default.moveItem(at: temporary, to: image)
                phase = "Verifying update…"; onChange?()
                try await Task.detached { try DiskieUpdateFiles.prepare(image: image, release: release, workspace: space) }.value
                guard let bundled = Bundle.main.url(forResource: "diskie-update-helper", withExtension: nil) else { throw DiskieUpdateError.command }
                let executable = space.appendingPathComponent("diskie-update-helper")
                try FileManager.default.copyItem(at: bundled, to: executable)
                try await Task.detached { try DiskieUpdateFiles.signature(executable, app: false, notarized: false) }.value
                let process = Process(); process.executableURL = executable
                process.arguments = ["--workspace", space.path, "--target", target.path, "--parent-pid", String(getpid()), "--version", release.version]
                process.standardInput = FileHandle.nullDevice; process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
                UserDefaults.standard.set(space.path, forKey: pendingKey)
                try process.run(); helper = process
                phase = "Preparing restart…"; onChange?()
                let deadline = Date().addingTimeInterval(150)
                while Date() < deadline {
                    if let data = try? Data(contentsOf: space.appendingPathComponent("result.json")),
                       let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        throw DiskieUpdateError.helper(result["message"] as? String ?? "The update could not be prepared. Your current app was kept.")
                    }
                    if FileManager.default.fileExists(atPath: space.appendingPathComponent("ready").path) {
                        handedOff = true; phase = "Restarting Diskie…"; onChange?(); onReadyToRestart?(); return
                    }
                    if !process.isRunning { throw DiskieUpdateError.command }
                    try await Task.sleep(nanoseconds: 100_000_000)
                }
                process.terminate()
                throw DiskieUpdateError.helper("Update preparation timed out. Your current app was kept.")
            } catch {
                if let helper = helper, helper.isRunning { helper.terminate(); helper.waitUntilExit() }
                self.helper = nil; UserDefaults.standard.removeObject(forKey: pendingKey)
                message("Could not install the update", detail: error.localizedDescription)
            }
        }
    }
    private func recoverPendingUpdate() {
        guard let path = UserDefaults.standard.string(forKey: pendingKey) else { return }
        let space = URL(fileURLWithPath: path, isDirectory: true)
        guard DiskieUpdateFiles.isWorkspace(space) else { UserDefaults.standard.removeObject(forKey: pendingKey); return }
        Task { @MainActor in
            for _ in 0..<120 {
                if let data = try? Data(contentsOf: space.appendingPathComponent("result.json")),
                   let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let success = result["success"] as? Bool {
                    UserDefaults.standard.removeObject(forKey: pendingKey)
                    if !success { message("Diskie kept the previous version", detail: result["message"] as? String ?? "The update could not complete.") }
                    try? FileManager.default.removeItem(at: space); return
                }
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
            // Keep the workspace and pending marker for diagnosing an interrupted helper.
        }
    }
}
