"""Fail-closed checks for the signed release asset gate."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "release_assets",
    Path(__file__).resolve().parents[1] / "scripts/release_assets.py",
)
assert SPEC is not None and SPEC.loader is not None
release_assets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_assets)

SOURCE = "a" * 40
VERSION = "1.2.1"
VERSIONED_NAME = f"diskie-{VERSION}-aarch64-apple-darwin.dmg"
DMG_BYTES = b"synthetic notarized disk image"
DMG_SHA256 = hashlib.sha256(DMG_BYTES).hexdigest()


class ReleaseAssetTests(unittest.TestCase):
    def make_assets(self, root: Path) -> None:
        root.mkdir()
        for name in (VERSIONED_NAME, "Diskie.dmg"):
            (root / name).write_bytes(DMG_BYTES)
            (root / f"{name}.sha256").write_text(f"{DMG_SHA256}  {name}\n")
        receipt = {
            "schema_version": 1,
            "source_revision": SOURCE,
            "version": VERSION,
            "dmg_sha256": DMG_SHA256,
            "application_inventory_sha256": "b" * 64,
            "identity": {"sha1": "A" * 40, "team_id": "ABCDEFGHIJ"},
            "application_notarization": {
                "status": "Accepted",
                "submission_id": "12345678-1234-1234-1234-123456789abc",
            },
            "disk_image_notarization": {
                "status": "Accepted",
                "submission_id": "abcdefab-cdef-abcd-efab-cdefabcdefab",
            },
            "application_ticket": "stapled",
            "disk_image_ticket": "stapled",
        }
        (root / "Diskie.dmg.notarization.json").write_text(json.dumps(receipt))

    def test_valid_five_asset_set_passes_and_writes_source_bound_notes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            self.make_assets(root)
            self.assertEqual(release_assets.validate_assets(root, SOURCE, VERSION)["version"], VERSION)
            notes = Path(temporary) / "notes.md"
            result = release_assets.main(
                ["--source", SOURCE, "--version", VERSION, "--assets", str(root), "--notes", str(notes)]
            )
            self.assertEqual(result, 0)
            self.assertIn(SOURCE, notes.read_text())
            self.assertIn(VERSION, notes.read_text())

    def assert_rejected_without_notes(self, mutate=None, source=SOURCE, version=VERSION):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "assets"
        self.make_assets(root)
        if mutate:
            mutate(root)
        notes = Path(temporary.name) / "release-notes.md"
        result = release_assets.main(
            ["--source", source, "--version", version, "--assets", str(root), "--notes", str(notes)]
        )
        self.assertEqual(result, 1)
        self.assertFalse(notes.exists())

    def test_source_and_version_mismatches_are_rejected_before_notes(self):
        self.assert_rejected_without_notes(source="c" * 40)
        self.assert_rejected_without_notes(version="1.2.2")

    def test_changed_versioned_dmg_alias_and_checksum_are_rejected(self):
        cases = (
            lambda root: (root / VERSIONED_NAME).write_bytes(b"changed image"),
            lambda root: (root / "Diskie.dmg").write_bytes(b"changed alias"),
            lambda root: (root / f"{VERSIONED_NAME}.sha256").write_text("0" * 64),
            lambda root: (root / "Diskie.dmg.sha256").write_text("0" * 64),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate):
                self.assert_rejected_without_notes(mutate)

    def test_missing_or_rejected_notarization_is_rejected_before_notes(self):
        def remove_receipt(root):
            (root / "Diskie.dmg.notarization.json").unlink()

        def reject_app(root):
            path = root / "Diskie.dmg.notarization.json"
            receipt = json.loads(path.read_text())
            receipt["application_notarization"]["status"] = "Rejected"
            path.write_text(json.dumps(receipt))

        def reject_image(root):
            path = root / "Diskie.dmg.notarization.json"
            receipt = json.loads(path.read_text())
            receipt["disk_image_notarization"]["status"] = "Rejected"
            path.write_text(json.dumps(receipt))

        for mutate in (remove_receipt, reject_app, reject_image):
            with self.subTest(mutate=mutate):
                self.assert_rejected_without_notes(mutate)

    def test_missing_invalid_symlink_and_extra_assets_are_rejected_before_notes(self):
        def remove_asset(root):
            (root / VERSIONED_NAME).unlink()

        def replace_with_directory(root):
            path = root / "Diskie.dmg.sha256"
            path.unlink()
            path.mkdir()

        def replace_with_symlink(root):
            path = root / "Diskie.dmg"
            path.unlink()
            path.symlink_to(root / VERSIONED_NAME)

        def add_extra(root):
            (root / "unexpected.txt").write_text("unexpected")

        for mutate in (remove_asset, replace_with_directory, replace_with_symlink, add_extra):
            with self.subTest(mutate=mutate):
                self.assert_rejected_without_notes(mutate)


if __name__ == "__main__":
    unittest.main()
