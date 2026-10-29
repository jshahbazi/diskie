"""Strict version selection and workflow-output checks."""

from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import release_version


class ReleaseVersionTests(unittest.TestCase):
    def test_numeric_ordering_ignores_prereleases_and_malformed_tags(self):
        tags = ["v0.1.2", "v0.9.9", "v0.10.3", "v9.0.0-rc.1", "latest", "v01.0.0"]
        for bump, expected in (
            ("major", "1.0.0"),
            ("minor", "0.11.0"),
            ("patch", "0.10.4"),
        ):
            with self.subTest(bump=bump):
                self.assertEqual(release_version.next_version("0.1.0", tags, bump), expected)

    def test_plist_baseline_is_lower_bound_and_empty_history_is_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = root / "Resources"
            resources.mkdir()
            with (resources / "Info.plist").open("wb") as output:
                plistlib.dump({"CFBundleShortVersionString": "2.3.4"}, output)
            with patch.object(release_version, "ROOT", root):
                self.assertEqual(release_version.current_baseline(), "2.3.4")
            self.assertEqual(release_version.next_version("2.3.4", ["v1.0.0"], "patch"), "2.3.5")
            self.assertEqual(release_version.next_version("0.1.0", [], "minor"), "0.2.0")

    def test_invalid_versions_and_bumps_fail(self):
        for baseline, bump in (
            ("0.1.0-rc.1", "patch"),
            ("01.2.3", "patch"),
            ("1.2", "patch"),
            ("1.2.3", "other"),
        ):
            with self.subTest(baseline=baseline, bump=bump), self.assertRaises(ValueError):
                release_version.next_version(baseline, [], bump)
        self.assertIsNone(release_version.parse_version("1.2.3-alpha"))
        self.assertIsNone(release_version.parse_version("01.2.3"))

    def test_workflow_ref_and_sha_are_checked(self):
        with patch.dict(os.environ, {"GITHUB_REF": "refs/heads/release"}, clear=True):
            with self.assertRaises(ValueError):
                release_version.verify_workflow_ref()
        with (
            patch.dict(os.environ, {"GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "bad"}, clear=True),
            patch.object(release_version.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "actual\n", "")),
        ):
            with self.assertRaises(ValueError):
                release_version.verify_workflow_ref()

    def test_selection_emits_output_without_creating_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def git(*arguments):
                return subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True, text=True).stdout

            git("init", "--quiet")
            git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "test")
            git("tag", "v0.1.2")
            resources = root / "Resources"
            resources.mkdir()
            with (resources / "Info.plist").open("wb") as output:
                plistlib.dump({"CFBundleShortVersionString": "0.1.0"}, output)
            output = root / "output"
            with (
                patch.object(release_version, "ROOT", root),
                patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}, clear=True),
                patch("sys.argv", ["release_version.py", "--bump", "patch"]),
            ):
                self.assertEqual(release_version.main(), 0)
            self.assertEqual(output.read_text(), "version=0.1.3\n")
            self.assertEqual(git("tag", "--list"), "v0.1.2\n")


if __name__ == "__main__":
    unittest.main()
