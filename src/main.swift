import Cocoa
import WebKit

final class App: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var button: NSButton!
    var cancelButton: NSButton!
    var status: NSTextField!
    var spinner: NSProgressIndicator!
    var process: Process?
    var cancelled = false
    var loaded = false
    var firstScan = false
    var progressTimer: Timer?
    var lastProgress: UInt64 = 0
    var lastProgressDate = Date()
    let fm = FileManager.default
    // Preserve existing snapshots when upgrading the original local prototype.
    var folder: URL {
        let support = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let legacy = support.appendingPathComponent("Disk Space Explorer", isDirectory: true)
        return fm.fileExists(atPath: legacy.path) ? legacy : support.appendingPathComponent("Diskie", isDirectory: true)
    }
    var snapshot: URL { folder.appendingPathComponent("snapshot.json") }
    var temp: URL { folder.appendingPathComponent("scan-in-progress.json") }
    var progress: URL { URL(fileURLWithPath: temp.path + ".progress") }
    func applicationDidFinishLaunching(_ notification: Notification) {
        let bounds = NSRect(x: 0, y: 0, width: 1120, height: 780)
        window = NSWindow(contentRect: bounds, styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Diskie"; window.minSize = NSSize(width: 660, height: 480); window.center()
        let container = NSView(frame: bounds); container.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = container
        let bar = NSView(); bar.translatesAutoresizingMaskIntoConstraints = false
        bar.wantsLayer = true; bar.layer?.backgroundColor = NSColor(calibratedRed: 0.055, green: 0.078, blue: 0.125, alpha: 1).cgColor
        button = NSButton(title: "Rescan", target: self, action: #selector(rescan)); button.bezelStyle = .rounded; button.translatesAutoresizingMaskIntoConstraints = false
        cancelButton = NSButton(title: "Cancel", target: self, action: #selector(cancel)); cancelButton.bezelStyle = .rounded; cancelButton.translatesAutoresizingMaskIntoConstraints = false; cancelButton.isHidden = true
        status = NSTextField(labelWithString: ""); status.textColor = .secondaryLabelColor; status.font = .systemFont(ofSize: 12); status.translatesAutoresizingMaskIntoConstraints = false
        spinner = NSProgressIndicator(); spinner.style = .spinning; spinner.controlSize = .small; spinner.translatesAutoresizingMaskIntoConstraints = false; spinner.isDisplayedWhenStopped = false
        [button, cancelButton, status, spinner].forEach { bar.addSubview($0!) }
        web = WKWebView(frame: .zero); web.navigationDelegate = self; web.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(bar); container.addSubview(web)
        NSLayoutConstraint.activate([bar.topAnchor.constraint(equalTo: container.topAnchor), bar.leadingAnchor.constraint(equalTo: container.leadingAnchor), bar.trailingAnchor.constraint(equalTo: container.trailingAnchor), bar.heightAnchor.constraint(equalToConstant: 50), button.leadingAnchor.constraint(equalTo: bar.leadingAnchor, constant: 20), button.centerYAnchor.constraint(equalTo: bar.centerYAnchor), cancelButton.leadingAnchor.constraint(equalTo: button.trailingAnchor, constant: 8), cancelButton.centerYAnchor.constraint(equalTo: bar.centerYAnchor), spinner.leadingAnchor.constraint(equalTo: cancelButton.trailingAnchor, constant: 12), spinner.centerYAnchor.constraint(equalTo: bar.centerYAnchor), status.leadingAnchor.constraint(equalTo: spinner.trailingAnchor, constant: 8), status.centerYAnchor.constraint(equalTo: bar.centerYAnchor), status.trailingAnchor.constraint(lessThanOrEqualTo: bar.trailingAnchor, constant: -16), web.topAnchor.constraint(equalTo: bar.bottomAnchor), web.leadingAnchor.constraint(equalTo: container.leadingAnchor), web.trailingAnchor.constraint(equalTo: container.trailingAnchor), web.bottomAnchor.constraint(equalTo: container.bottomAnchor)])
        do { try fm.createDirectory(at: folder, withIntermediateDirectories: true) } catch { showError("Unable to create the app’s data folder: \(error.localizedDescription)") }
        firstScan = !fm.fileExists(atPath: snapshot.path)
        guard let chart = Bundle.main.url(forResource: "chart", withExtension: "html") else { showError("The chart is missing from the app."); return }
        web.loadFileURL(chart, allowingReadAccessTo: chart.deletingLastPathComponent())
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loaded = true
        do {
            if fm.fileExists(atPath: snapshot.path) { try showSnapshot(Data(contentsOf: snapshot)) }
            else {
                let attributes = try fm.attributesOfFileSystem(forPath: "/System/Volumes/Data")
                let empty: [String: Any] = [
                    "tree": ["name": "Data volume", "size": 0, "children": []] as [String: Any],
                    "total": attributes[.systemSize] ?? 0, "free": attributes[.systemFreeSize] ?? 0,
                    "errors": [:] as [String: Int], "date": "No scan yet"
                ]
                try showSnapshot(JSONSerialization.data(withJSONObject: empty))
            }
        } catch { showError("Could not load the saved scan: \(error.localizedDescription)") }
        if firstScan { status.stringValue = "Starting first scan…"; rescan() } else { status.stringValue = "Saved snapshot" }
    }
    func showSnapshot(_ data: Data) throws {
        _ = try JSONSerialization.jsonObject(with: data)
        let encoded = data.base64EncodedString()
        web.evaluateJavaScript("window.showData(JSON.parse(new TextDecoder().decode(Uint8Array.from(atob('\(encoded)'), c => c.charCodeAt(0)))))") { _, error in if let error = error { self.showError("Could not display the scan: \(error.localizedDescription)") } }
    }
    @objc func rescan() {
        guard process == nil else { return }
        guard let executable = Bundle.main.url(forResource: "disk-scanner", withExtension: nil) else { showError("The scanner is missing from the app."); return }
        cleanTemporaryFiles(); cancelled = false
        lastProgress = 0; lastProgressDate = Date()
        let task = Process(); task.executableURL = executable; task.arguments = [temp.path]
        task.standardError = FileHandle.nullDevice; task.standardOutput = FileHandle.nullDevice
        task.terminationHandler = { p in
            DispatchQueue.main.async {
                self.progressTimer?.invalidate(); self.progressTimer = nil
                self.process = nil; self.button.isEnabled = true; self.cancelButton.isHidden = true; self.spinner.stopAnimation(nil)
                defer { self.cleanTemporaryFiles(); self.status.toolTip = nil }
                if self.cancelled { self.status.stringValue = "Scan cancelled; showing previous snapshot"; return }
                guard p.terminationStatus == 0 else { self.status.stringValue = "Scan failed; showing previous snapshot"; self.showError("The disk scan failed (exit \(p.terminationStatus)). Your previous snapshot is still available."); return }
                do {
                    let data = try Data(contentsOf: self.temp)
                    let result = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                    try data.write(to: self.snapshot, options: .atomic)
                    try self.showSnapshot(data)
                    let seconds = (result?["seconds"] as? NSNumber)?.doubleValue ?? 0
                    let errors = (result?["errors"] as? [String: NSNumber])?.values.reduce(0) { $0 + $1.intValue } ?? 0
                    let coverage = errors > 0 ? " · \(errors.formatted()) unreadable paths" : ""
                    self.status.stringValue = "Scan complete in \(self.duration(seconds))\(coverage)"
                } catch { self.status.stringValue = "Scan failed; showing previous snapshot"; self.showError("Could not save the scan: \(error.localizedDescription)") }
            }
        }
        do {
            try task.run(); process = task; button.isEnabled = false; cancelButton.isHidden = false
            spinner.startAnimation(nil); status.stringValue = "Scanning the data volume…"
            progressTimer = Timer.scheduledTimer(withTimeInterval: 0.75, repeats: true) { [weak self] _ in self?.updateProgress() }
        }
        catch { showError("Could not start the scan: \(error.localizedDescription)") }
    }
    func duration(_ seconds: Double) -> String {
        let value = max(0, Int(seconds))
        return value < 60 ? "\(value)s" : "\(value / 60)m \(value % 60)s"
    }
    func cleanTemporaryFiles() {
        for path in [temp, progress, URL(fileURLWithPath: progress.path + ".new")] { try? fm.removeItem(at: path) }
    }
    func updateProgress() {
        guard process != nil, !cancelled,
              let data = try? Data(contentsOf: progress),
              let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let count = (result["files"] as? NSNumber)?.uint64Value,
              let directories = (result["directories"] as? NSNumber)?.uint64Value,
              let workers = (result["workers"] as? NSNumber)?.intValue else { return }
        if count + directories != lastProgress { lastProgress = count + directories; lastProgressDate = Date() }
        let seconds = (result["seconds"] as? NSNumber)?.doubleValue ?? 0
        let waiting = Date().timeIntervalSince(lastProgressDate) > 15 ? " · waiting on a folder" : ""
        status.stringValue = "Scanning… \(count.formatted()) files · \(workers) workers · \(duration(seconds))\(waiting)"
        status.toolTip = result["path"] as? String
    }
    @objc func cancel() { guard let p = process else { return }; cancelled = true; status.stringValue = "Stopping scan…"; p.terminate() }
    func showError(_ text: String) { let alert = NSAlert(); alert.messageText = "Diskie"; alert.informativeText = text; alert.alertStyle = .warning; alert.runModal() }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) {
        progressTimer?.invalidate()
        if let p = process, p.isRunning { p.terminate() }
    }
}
let app = NSApplication.shared
let delegate = App(); app.delegate = delegate; app.setActivationPolicy(.regular)
let menu = NSMenu(); let appMenuItem = NSMenuItem(); menu.addItem(appMenuItem)
let appMenu = NSMenu(); appMenu.addItem(withTitle: "Quit Diskie", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"); appMenuItem.submenu = appMenu; app.mainMenu = menu
app.run()
