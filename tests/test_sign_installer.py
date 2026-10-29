"""Focused fail-closed tests for the signed installer recipe."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "sign_installer",
    Path(__file__).resolve().parents[1] / "scripts/sign_installer.py",
)
assert SPEC is not None and SPEC.loader is not None
signer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(signer)

IDENTITY = {"sha1": "A" * 40, "team_id": "ABCDEFGHIJ"}


class SignInstallerTests(unittest.TestCase):
    def test_identity_resolution_rejects_non_developer_id(self):
        for title in (
            '1) ' + "A" * 40 + ' "Apple Development: Dev (ABCDEFGHIJ)"',
            '1) ' + "A" * 40 + ' "Developer ID Installer: Dev (ABCDEFGHIJ)"',
            '1) ' + "A" * 40 + ' "Developer ID Application: Dev (ABC)"',
        ):
            with self.subTest(title=title), patch.object(
                signer, "run", return_value=type("Result", (), {"stdout": (title + "\n").encode()})()
            ):
                with self.assertRaisesRegex(signer.ReleaseError, "developer_id_identity_required"):
                    signer.resolve_identity("A" * 40, "/tmp/keychain")

    def test_notarization_requires_accepted_status_and_well_formed_receipt(self):
        for payload in (
            b'{"status":"Rejected","id":"12345678-1234-1234-1234-123456789abc"}',
            b'{"status":"Accepted","id":"bad"}',
            b'{"status":"Accepted"}',
            b'{malformed',
        ):
            with self.subTest(payload=payload), patch.object(
                signer, "run", return_value=type("Result", (), {"stdout": payload})()
            ):
                with self.assertRaises(signer.ReleaseError):
                    signer.notarize("archive", "keychain", "profile")

    def test_signature_requires_apple_identity_team_timestamp_and_runtime(self):
        base = (
            "Executable=/tmp/app\nTeamIdentifier=ABCDEFGHIJ\nTimestamp=2026-10-28T00:00:00Z\n"
            "CodeDirectory v=20500 flags=0x10000(runtime)\n"
        )

        def run_for(detail: str, entitlements: bytes = b""):
            def run(arguments, stage, timeout=120):
                if arguments[1:3] == ["--display", "--verbose=4"]:
                    return type("Result", (), {"stderr": detail.encode(), "stdout": b""})()
                if arguments[1:3] == ["--display", "--entitlements"]:
                    return type("Result", (), {"stderr": b"", "stdout": entitlements})()
                return type("Result", (), {"stderr": b"", "stdout": b""})()

            return run

        for detail, entitlements, stage in (
            (base.replace("TeamIdentifier=ABCDEFGHIJ", "TeamIdentifier=ZZZZZZZZZZ"), b"", "signing_team_mismatch"),
            (base.replace("Timestamp=2026-10-28T00:00:00Z", "Timestamp=none"), b"", "secure_timestamp_missing"),
            (base.replace("flags=0x10000(runtime)", "flags=0x2(adhoc)"), b"", "hardened_runtime_missing"),
            (base, b"<plist><key>example</key></plist>", "unexpected_entitlements"),
        ):
            with self.subTest(stage=stage), patch.object(
                signer, "run", side_effect=run_for(detail, entitlements)
            ):
                with self.assertRaisesRegex(signer.ReleaseError, stage):
                    signer.verify_signature("/tmp/app", IDENTITY)
        with patch.object(signer, "run", side_effect=run_for(base)) as run:
            signer.verify_signature("/tmp/diskie.dmg", IDENTITY, runtime=False)
            self.assertFalse(any("--entitlements" in call.args[0] for call in run.call_args_list))

    def test_source_receipt_and_digest_fail_before_image_mount(self):
        for receipt, expected_source, expected_error in (
            ({"source_revision": "b" * 40}, "a" * 40, "source_receipt_mismatch"),
            ({"source_revision": "a" * 40, "version": "1.2.3", "dmg_sha256": "0" * 64}, "a" * 40, "installer_digest_mismatch"),
        ):
            with self.subTest(error=expected_error), tempfile.TemporaryDirectory() as temporary:
                image = Path(temporary) / "Diskie.dmg"
                image.write_bytes(b"synthetic disk image")
                with patch.object(signer, "run") as run:
                    with self.assertRaisesRegex(signer.ReleaseError, expected_error):
                        signer.verify_installer(image, receipt, expected_source)
                    run.assert_not_called()

    def test_verify_only_uses_receipt_without_credentials_or_keychain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "Diskie.dmg"
            image.write_bytes(b"synthetic disk image")
            receipt = {
                "source_revision": "A" * 40,
                "version": "1.2.3",
                "dmg_sha256": signer.digest(image),
                "identity": IDENTITY,
            }
            (root / "Diskie.dmg.notarization.json").write_text(json.dumps(receipt))
            with (
                patch.dict("os.environ", {}, clear=True),
                patch.object(signer, "verify_installer") as verify,
            ):
                self.assertEqual(signer.main(["--source", "A" * 40, "--output", str(root), "--verify-only"]), 0)
                verify.assert_called_once_with(image, receipt, "A" * 40)

    def test_diagnostics_are_fixed_and_do_not_echo_sensitive_exception(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), patch.object(
            signer, "verify_installer", side_effect=RuntimeError("private key bytes")
        ):
            result = signer.main(["--source", "A" * 40, "--output", "/tmp/out", "--verify-only"])
        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "diskie_installer_failed:invalid_release_data\n")
        self.assertNotIn("private key", output.getvalue())


if __name__ == "__main__":
    unittest.main()
