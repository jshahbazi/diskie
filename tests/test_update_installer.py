"""Exercise native update safety primitives without touching installed apps."""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
import Foundation
import Darwin

let args = Array(CommandLine.arguments.dropFirst())
do {
    switch args[0] {
    case "version":
        let left = try InstallVersion(args[1], legacy: args[3] == "legacy")
        let right = try InstallVersion(args[2], legacy: true)
        print(left == right ? "equal" : (left < right ? "older" : "newer"))
    case "physical":
        print(try installPhysicalPath(URL(fileURLWithPath: args[1])).path)
    case "workspace":
        try installValidateWorkspace(URL(fileURLWithPath: args[1], isDirectory: true)); print("valid")
    case "target":
        try installValidateTargetPath(URL(fileURLWithPath: args[1], isDirectory: true)); print("valid")
    case "bundle":
        _ = try installValidateBundle(URL(fileURLWithPath: args[1], isDirectory: true), expectedVersion: args[2], gatekeeper: false)
        print("valid")
    case "lock":
        let lock = try InstallTargetLock(parent: URL(fileURLWithPath: args[1], isDirectory: true), targetName: "Diskie.app")
        print("locked"); fflush(stdout)
        if args.count > 2 { Thread.sleep(forTimeInterval: Double(args[2])!) }
        withExtendedLifetime(lock) {}
    case "exchange":
        try installExchange(URL(fileURLWithPath: args[1]), URL(fileURLWithPath: args[2])); print("exchanged")
    case "activate":
        let staged = URL(fileURLWithPath: args[1]), target = URL(fileURLWithPath: args[2])
        try installActivate(staged: staged, target: target, beforeExchange: {
            if args[3] == "reopened" {
                guard let process = InstallProcessIdentity(getpid()) else { throw UpdateInstallError(message: "No test process") }
                try installRequireNoProcesses(at: [process.path])
            }
        }, launch: {
            try installRequire(try String(contentsOf: target.appendingPathComponent("version"), encoding: .utf8) == "new", "Wrong active app")
            if args[3] != "success" { throw UpdateInstallError(message: "Injected launch failure") }
        }, verifyRecovery: {
            if args[3] == "recovery-refused" { throw UpdateInstallError(message: "Injected verification failure") }
        }, stopFailedLaunch: {})
        print("activated")
    case "timeout":
        try installCommand("/bin/sleep", ["5"], "Sleep failed.", timeout: 0.1)
    case "enumeration-error":
        _ = try installProcesses(at: "/fake", listPIDs: { buffer, size in buffer == nil ? 1 : -1 })
    case "enumeration-truncated":
        _ = try installProcesses(at: "/fake", listPIDs: { buffer, size in buffer == nil ? 1 : size / Int32(MemoryLayout<pid_t>.stride) })
    case "process":
        guard let identity = InstallProcessIdentity(getpid()) else { throw UpdateInstallError(message: "Missing process identity") }
        try installRequire(identity.uid == getuid() && identity.pid == getpid() && (try installProcesses(at: identity.path)).contains(identity), "Process identity mismatch")
        print("valid")
    default: exit(2)
    }
} catch {
    print(error.localizedDescription); exit(1)
}
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "requires native macOS Swift tools")
class NativeUpdateInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compilation = tempfile.TemporaryDirectory(prefix="diskie-installer-tests-")
        folder = Path(cls.compilation.name)
        harness = folder / "main.swift"
        harness.write_text(HARNESS)
        cls.harness = folder / "test-helper"
        cls.helper = folder / "diskie-update-helper"
        common = ["swiftc", "-swift-version", "5", str(ROOT / "src/UpdateInstaller.swift")]
        subprocess.run(
            common + ["-D", "DISKIE_UPDATER_TESTING", str(harness), "-o", str(cls.harness), "-framework", "AppKit", "-framework", "Security"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            common + ["-parse-as-library", "-o", str(cls.helper), "-framework", "AppKit", "-framework", "Security"],
            check=True, capture_output=True, text=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.compilation.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="diskie-update-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        self.folder.chmod(0o700)

    def run_harness(self, *arguments):
        return subprocess.run([str(self.harness), *map(str, arguments)], capture_output=True, text=True, timeout=20)

    def test_numeric_versions_and_legacy_installed_versions(self):
        for left, right, mode, expected in [
            ("1.10.0", "1.9.9", "strict", "newer"),
            ("1.2.0", "1.2", "strict", "equal"),
            ("1.2", "1.2.1", "legacy", "older"),
            ("2", "1.99.99", "legacy", "newer"),
        ]:
            with self.subTest(left=left, right=right):
                result = self.run_harness("version", left, right, mode)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), expected)
        for invalid in ["1.2", "01.2.3", "1.2.3-beta", "1.2.3.4", "1.2.9999999999", "1..3", "1.2.-3"]:
            self.assertNotEqual(self.run_harness("version", invalid, "1.2.3", "strict").returncode, 0)

    def test_physical_paths_reject_symbolic_link_ancestors(self):
        real = self.folder / "real"
        real.mkdir()
        (real / "child").mkdir()
        link = self.folder / "link"
        link.symlink_to(real, target_is_directory=True)
        self.assertEqual(self.run_harness("physical", real / "child").returncode, 0)
        self.assertNotEqual(self.run_harness("physical", link / "child").returncode, 0)
        self.assertNotEqual(self.run_harness("physical", self.folder / "absent").returncode, 0)

    def test_workspace_requires_private_mode_and_temporary_location(self):
        self.assertEqual(self.run_harness("workspace", self.folder).returncode, 0)
        self.folder.chmod(0o755)
        result = self.run_harness("workspace", self.folder)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workspace is invalid", result.stdout)

    def test_arbitrary_target_refused_before_app_mutation(self):
        target = self.folder / "Diskie.app"
        target.mkdir()
        sentinel = target / "keep"
        sentinel.write_text("original")
        result = subprocess.run([
            str(self.helper), "--workspace", str(self.folder), "--target", str(target),
            "--parent-pid", str(os.getpid()), "--version", "1.2.3",
        ], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(), "original")
        self.assertFalse((self.folder / "ready").exists())
        outcome = json.loads((self.folder / "result.json").read_text())
        self.assertFalse(outcome["success"])
        self.assertIn("Install Diskie in Applications", outcome["message"])

    def test_ad_hoc_candidate_is_rejected_by_developer_trust_policy(self):
        app = self.folder / "candidate.app"
        executable = app / "Contents/MacOS/Diskie"
        executable.parent.mkdir(parents=True)
        shutil.copyfile("/usr/bin/true", executable)
        executable.chmod(0o755)
        with (app / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleIdentifier": "local.diskspace.explorer", "CFBundleExecutable": "Diskie", "CFBundleShortVersionString": "1.2.3", "CFBundlePackageType": "APPL"}, stream)
        subprocess.run(["codesign", "--force", "--sign", "-", str(app)], capture_output=True, check=True)
        result = self.run_harness("bundle", app, "1.2.3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match Diskie", result.stdout)

    def test_lock_refuses_concurrent_updates_and_symbolic_links(self):
        holder = subprocess.Popen([str(self.harness), "lock", str(self.folder), "2"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            result = self.run_harness("lock", self.folder)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("in progress", result.stdout)
            self.assertEqual(holder.wait(timeout=10), 0)
        finally:
            if holder.poll() is None:
                holder.terminate(); holder.wait(timeout=10)
            holder.stdout.close(); holder.stderr.close()
        lock = self.folder / ".Diskie.app.update-lock"
        self.assertFalse(lock.exists())
        sentinel = self.folder / "unrelated"
        sentinel.write_text("keep")
        lock.symlink_to(sentinel)
        self.assertNotEqual(self.run_harness("lock", self.folder).returncode, 0)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_atomic_exchange_can_restore_original_and_failed_exchange_preserves_it(self):
        target, staged = self.folder / "target.app", self.folder / "staged.app"
        target.mkdir(); staged.mkdir()
        (target / "version").write_text("old")
        (staged / "version").write_text("new")
        self.assertEqual(self.run_harness("exchange", target, staged).returncode, 0)
        self.assertEqual((target / "version").read_text(), "new")
        self.assertEqual((staged / "version").read_text(), "old")
        self.assertEqual(self.run_harness("exchange", target, staged).returncode, 0)
        self.assertEqual((target / "version").read_text(), "old")
        self.assertNotEqual(self.run_harness("exchange", target, self.folder / "absent").returncode, 0)
        self.assertEqual((target / "version").read_text(), "old")

    def test_running_process_identity_includes_launch_time_and_uid(self):
        result = self.run_harness("process")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_activation_rolls_back_failed_launch_and_preserves_backup_when_recovery_refused(self):
        for mode in ["success", "launch-failed", "recovery-refused", "reopened"]:
            with self.subTest(mode=mode):
                directory = self.folder / mode
                directory.mkdir()
                target, staged = directory / "target.app", directory / "staged.app"
                target.mkdir(); staged.mkdir()
                (target / "version").write_text("old")
                (staged / "version").write_text("new")
                result = self.run_harness("activate", staged, target, mode)
                if mode == "success":
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual((target / "version").read_text(), "new")
                    self.assertEqual((staged / "version").read_text(), "old")
                elif mode == "launch-failed":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Injected launch failure", result.stdout)
                    self.assertEqual((target / "version").read_text(), "old")
                    self.assertEqual((staged / "version").read_text(), "new")
                elif mode == "recovery-refused":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("previous app remains", result.stdout)
                    self.assertEqual((target / "version").read_text(), "new")
                    self.assertEqual((staged / "version").read_text(), "old")
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("was reopened", result.stdout)
                    self.assertEqual((target / "version").read_text(), "old")
                    self.assertEqual((staged / "version").read_text(), "new")

    def test_external_commands_have_a_bounded_timeout(self):
        result = self.run_harness("timeout")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stdout)

    def test_process_enumeration_error_or_truncation_refuses_update(self):
        for mode in ["enumeration-error", "enumeration-truncated"]:
            with self.subTest(mode=mode):
                result = self.run_harness(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("update was not installed", result.stdout)


if __name__ == "__main__":
    unittest.main()
