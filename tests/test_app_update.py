"""Focused Swift contract checks for Diskie's release updater."""

from __future__ import annotations

import os
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


SWIFT_HARNESS = r'''
import Cocoa
import CryptoKit
import Foundation

func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { fatalError(message) }
}

func expectFailure(_ message: String, _ operation: () throws -> Void) {
    do { try operation(); fatalError("Expected rejection: \(message)") }
    catch is DiskieUpdateError { return }
    catch { fatalError("Unexpected error for \(message): \(error)") }
}

func releaseJSON(tag: String = "v1.3.0", draft: Bool = false, prerelease: Bool = false,
                 assetName: String = "diskie-1.3.0-aarch64-apple-darwin.dmg",
                 address: String = "https://github.com/jshahbazi/diskie/releases/download/v1.3.0/diskie-1.3.0-aarch64-apple-darwin.dmg",
                 digest: String? = "sha256:" + String(repeating: "a", count: 64),
                 size: Int = 1024, duplicates: Int = 1) throws -> DiskieRelease {
    let asset: [String: Any] = ["name": assetName, "browser_download_url": address,
                                "digest": digest.map { $0 as Any } ?? NSNull(), "size": size]
    let assets = Array(repeating: asset, count: duplicates)
    let data = try JSONSerialization.data(withJSONObject: ["tag_name": tag, "draft": draft,
        "prerelease": prerelease, "assets": assets])
    return try JSONDecoder().decode(DiskieRelease.self, from: data)
}

func plist(_ app: URL, version: String = "1.3.0", name: String = "Diskie",
           executableName: String = "Diskie", minimum: String = "13.0",
           header: Data = Data([0xcf, 0xfa, 0xed, 0xfe, 0x0c, 0, 0, 1])) throws {
    let contents = app.appendingPathComponent("Contents", isDirectory: true)
    let executable = contents.appendingPathComponent("MacOS/Diskie")
    try FileManager.default.createDirectory(at: executable.deletingLastPathComponent(),
                                            withIntermediateDirectories: true)
    try header.write(to: executable)
    let info: [String: Any] = ["CFBundleIdentifier": DiskieUpdateFiles.appID,
        "CFBundleExecutable": executableName, "CFBundleName": name,
        "CFBundleShortVersionString": version, "LSMinimumSystemVersion": minimum]
    let data = try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
    try data.write(to: contents.appendingPathComponent("Info.plist"))
}

@main
struct UpdaterChecks {
    static func main() throws {
        // Published release semantics, including abbreviated installed versions.
        let newer = try releaseJSON().update(after: "1.2")
        require(newer?.version == "1.3.0" && newer?.size == 1024, "new stable release not selected")
        let equal = try releaseJSON(tag: "v1.2.0").update(after: "1.2.0")
        let older = try releaseJSON(tag: "v1.2.0").update(after: "1.3.0")
        require(equal == nil, "equal release should not be an update")
        require(older == nil, "older release should not be an update")
        for (label, release, current) in [
            ("malformed tag", try releaseJSON(tag: "release-1.3.0"), "1.2.0"),
            ("prerelease", try releaseJSON(prerelease: true), "1.2.0"),
            ("draft", try releaseJSON(draft: true), "1.2.0"),
            ("malformed installed version", try releaseJSON(), "01.2.0"),
            ("wrong asset name", try releaseJSON(assetName: "other.dmg"), "1.2.0"),
            ("wrong download URL", try releaseJSON(address: "https://attacker.invalid/update.dmg"), "1.2.0"),
            ("missing digest", try releaseJSON(digest: nil), "1.2.0"),
            ("uppercase digest", try releaseJSON(digest: "sha256:" + String(repeating: "A", count: 64)), "1.2.0"),
            ("short digest", try releaseJSON(digest: "sha256:abc"), "1.2.0"),
            ("oversized asset", try releaseJSON(size: 268_435_457), "1.2.0"),
            ("zero size", try releaseJSON(size: 0), "1.2.0"),
            ("duplicate matching assets", try releaseJSON(duplicates: 2), "1.2.0")
        ] {
            expectFailure(label) { _ = try release.update(after: current) }
        }

        // Redirect policy is origin-sensitive and rejects credential/port tricks.
        for address in ["https://github.com/owner/repo/releases/latest",
                        "https://api.github.com/repos/owner/repo/releases/latest",
                        "https://objects.githubusercontent.com/release/file"] {
            require(DiskieRedirects.allowed(URL(string: address)), "trusted redirect rejected: \(address)")
        }
        for address in ["http://github.com/owner/repo", "https://github.com.evil.invalid/x",
                        "https://github.com@evil.invalid/x", "https://user:pass@github.com/x",
                        "https://github.com:444/x", "file:///tmp/release.dmg"] {
            require(!DiskieRedirects.allowed(URL(string: address)), "untrusted redirect accepted: \(address)")
        }
        let delegate = DiskieRedirects()
        let response = HTTPURLResponse(url: URL(string: "https://github.com/old")!, statusCode: 302,
                                       httpVersion: nil, headerFields: nil)!
        var redirected: URLRequest?
        delegate.urlSession(URLSession.shared, task: URLSession.shared.dataTask(with: response.url!),
                            willPerformHTTPRedirection: response,
                            newRequest: URLRequest(url: URL(string: "https://evil.invalid/new")!)) {
            redirected = $0
        }
        require(redirected == nil, "delegate followed a foreign redirect")

        // Mach-O parsing checks both thin and fat headers and rejects truncation.
        require(DiskieUpdateFiles.supportsArm64(Data([0xcf, 0xfa, 0xed, 0xfe, 0x0c, 0, 0, 1])),
                "valid little-endian arm64 header rejected")
        require(DiskieUpdateFiles.supportsArm64(Data([0xfe, 0xed, 0xfa, 0xcf, 1, 0, 0, 0x0c])),
                "valid big-endian arm64 header rejected")
        require(!DiskieUpdateFiles.supportsArm64(Data([0xcf, 0xfa, 0xed, 0xfe, 7, 0, 0, 1])),
                "wrong thin architecture accepted")
        var fat = Data([0xca, 0xfe, 0xba, 0xbe, 0, 0, 0, 1, 1, 0, 0, 0x0c])
        fat.append(Data(repeating: 0, count: 16))
        require(DiskieUpdateFiles.supportsArm64(fat), "valid fat arm64 header rejected")
        require(!DiskieUpdateFiles.supportsArm64(Data([0xca, 0xfe, 0xba, 0xbe, 0xff, 0xff, 0xff, 0xff])),
                "oversized fat architecture count accepted")
        require(!DiskieUpdateFiles.supportsArm64(Data([0xcf, 0xfa, 0xed])), "truncated Mach-O header accepted")

        let root = FileManager.default.temporaryDirectory.appendingPathComponent("diskie-updater-test-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: root) }
        let image = root.appendingPathComponent("release.dmg")
        let bytes = Data("release image fixture".utf8)
        try bytes.write(to: image)
        let hash = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        let matching = DiskieDownload(version: "1.3.0", url: image, sha256: hash, size: Int64(bytes.count))
        try DiskieUpdateFiles.verifyDigest(image, release: matching)
        expectFailure("digest mismatch") {
            try DiskieUpdateFiles.verifyDigest(image, release: DiskieDownload(version: "1.3.0", url: image,
                sha256: String(repeating: "0", count: 64), size: Int64(bytes.count)))
        }
        expectFailure("size mismatch") {
            try DiskieUpdateFiles.verifyDigest(image, release: DiskieDownload(version: "1.3.0", url: image,
                sha256: hash, size: Int64(bytes.count + 1)))
        }

        let app = root.appendingPathComponent("Diskie.app", isDirectory: true)
        try plist(app)
        try DiskieUpdateFiles.metadata(app, version: "1.3.0")
        for (label, version, name, executableName, minimum, header) in [
            ("wrong version", "1.2.0", "Diskie", "Diskie", "13.0", Data([0xcf, 0xfa, 0xed, 0xfe, 0x0c, 0, 0, 1])),
            ("wrong app name", "1.3.0", "Other", "Diskie", "13.0", Data([0xcf, 0xfa, 0xed, 0xfe, 0x0c, 0, 0, 1])),
            ("wrong executable", "1.3.0", "Diskie", "Other", "13.0", Data([0xcf, 0xfa, 0xed, 0xfe, 0x0c, 0, 0, 1])),
            ("unsupported minimum OS", "1.3.0", "Diskie", "Diskie", "99.0", Data([0xcf, 0xfa, 0xed, 0xfe, 0x0c, 0, 0, 1])),
            ("wrong architecture", "1.3.0", "Diskie", "Diskie", "13.0", Data([0xcf, 0xfa, 0xed, 0xfe, 7, 0, 0, 1]))
        ] {
            try? FileManager.default.removeItem(at: app)
            try plist(app, version: version, name: name, executableName: executableName,
                      minimum: minimum, header: header)
            expectFailure(label) { try DiskieUpdateFiles.metadata(app, version: "1.3.0") }
        }
        print("Diskie updater contract checks passed")
    }
}
'''


@unittest.skipUnless(os.name == "posix" and os.uname().sysname == "Darwin" and platform.machine() == "arm64",
                     "Diskie release updater is macOS arm64 only")
class AppUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="diskie-app-update-tests-")
        root = Path(cls.temporary.name)
        harness = root / "UpdaterChecks.swift"
        cls.executable = root / "updater-checks"
        harness.write_text(SWIFT_HARNESS)
        completed = subprocess.run(
            ["swiftc", "-swift-version", "5", "-parse-as-library",
             str(ROOT / "src/AppUpdate.swift"), str(harness), "-framework", "Cocoa",
             "-o", str(cls.executable)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(
                "Could not compile updater contract harness:\n"
                + completed.stdout + completed.stderr
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_release_validation_redirects_digest_and_metadata(self) -> None:
        completed = subprocess.run(
            [str(self.executable)], text=True, capture_output=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("Diskie updater contract checks passed", completed.stdout)
