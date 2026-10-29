"""Recovery and output-boundary checks for the hosted Apple credential helper."""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "apple_credentials",
    Path(__file__).resolve().parents[1] / ".github/scripts/apple_credentials.py",
)
assert SPEC is not None and SPEC.loader is not None
credentials = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(credentials)


def environment(root: Path) -> dict[str, str]:
    return {
        "RUNNER_TEMP": str(root),
        "MACOS_CERTIFICATE_P12_BASE64": base64.b64encode(b"synthetic p12").decode(),
        "MACOS_CERTIFICATE_PASSWORD": "synthetic certificate password",
        "MACOS_SIGNING_IDENTITY": "Developer ID Application: Test (ABCDEFGHIJ)",
        "APPLE_API_KEY_P8_BASE64": base64.b64encode(b"synthetic p8").decode(),
        "APPLE_API_KEY_ID": "SYNTHETIC1",
        "APPLE_API_ISSUER_ID": "00000000-0000-0000-0000-000000000000",
    }


class AppleCredentialTests(unittest.TestCase):
    def test_private_files_refuse_replacement_and_use_owner_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private"
            credentials.write_private(path, b"synthetic")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                credentials.write_private(path, b"replacement")
            self.assertEqual(path.read_bytes(), b"synthetic")

    def test_missing_or_wrong_credentials_fail_before_keychain_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for changes in (
                {"APPLE_API_KEY_ID": ""},
                {"MACOS_SIGNING_IDENTITY": "Apple Development: Test"},
                {"MACOS_CERTIFICATE_P12_BASE64": "invalid!"},
            ):
                with (
                    self.subTest(changes=changes),
                    patch.dict(os.environ, environment(root) | changes, clear=True),
                    patch.object(credentials, "run") as run,
                ):
                    with self.assertRaises(credentials.CredentialError):
                        credentials.setup()
                    run.assert_not_called()
                    self.assertFalse((root / "diskie-signing").exists())

    def test_setup_preserves_search_list_and_removes_credential_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = ["/a/login.keychain-db", "/a/other keychain.keychain-db"]
            commands = []

            def run(*arguments):
                commands.append(arguments)
                if (
                    arguments[1:4] == ("list-keychains", "-d", "user")
                    and "-s" not in arguments
                ):
                    return b'"/a/login.keychain-db"\n"/a/other keychain.keychain-db"\n'
                if arguments[1] == "create-keychain":
                    Path(arguments[-1]).touch()
                if arguments[1] == "import":
                    self.assertEqual(Path(arguments[2]).read_bytes(), b"synthetic p12")
                if arguments[1:3] == ("notarytool", "store-credentials"):
                    key = Path(arguments[arguments.index("--key") + 1])
                    self.assertEqual(key.read_bytes(), b"synthetic p8")
                return b""

            with (
                patch.dict(os.environ, environment(root), clear=True),
                patch.object(credentials, "run", side_effect=run),
            ):
                credentials.setup()
                managed = root / "diskie-signing"
                self.assertEqual(stat.S_IMODE(managed.stat().st_mode), 0o700)
                self.assertEqual(
                    json.loads((managed / credentials.SEARCH_LIST).read_bytes()),
                    original,
                )
                self.assertFalse((managed / "certificate.p12").exists())
                self.assertFalse((managed / "AuthKey.p8").exists())
                self.assertFalse((managed / "signing-probe.c").exists())
                credentials.cleanup()
                self.assertFalse(managed.exists())
            searches = [
                command
                for command in commands
                if command[1:4] == ("list-keychains", "-d", "user") and "-s" in command
            ]
            self.assertEqual(searches[-1][5:], tuple(original))
            self.assertTrue(
                any(command[1] == "delete-keychain" for command in commands)
            )

    def test_partial_setup_restores_search_list_and_deletes_created_keychain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = []

            def run(*arguments):
                commands.append(arguments)
                if arguments[1] == "list-keychains" and "-s" not in arguments:
                    return b'"/original/login.keychain-db"\n'
                if arguments[1] == "create-keychain":
                    Path(arguments[-1]).touch()
                if arguments[1] == "import":
                    raise credentials.CredentialError(
                        "synthetic secret tool diagnostic"
                    )
                return b""

            output = io.StringIO()
            with (
                patch.dict(os.environ, environment(root), clear=True),
                patch.object(credentials, "run", side_effect=run),
                contextlib.redirect_stderr(output),
            ):
                self.assertEqual(credentials.main(["setup"]), 1)
            self.assertEqual(output.getvalue(), "apple_credentials_setup_failed\n")
            self.assertFalse((root / "diskie-signing").exists())
            self.assertIn(
                (
                    "/usr/bin/security",
                    "list-keychains",
                    "-d",
                    "user",
                    "-s",
                    "/original/login.keychain-db",
                ),
                commands,
            )
            self.assertTrue(
                any(command[1] == "delete-keychain" for command in commands)
            )

    def test_cleanup_attempts_keychain_deletion_after_restoration_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = root / "diskie-signing"
            managed.mkdir()
            (managed / credentials.MARKER).write_bytes(b"diskie-signing-v1\n")
            (managed / credentials.SEARCH_LIST).write_text(
                '["/original/login.keychain-db"]'
            )
            (managed / "signing.keychain-db").touch()
            commands = []

            def run(*arguments):
                commands.append(arguments)
                if arguments[1] == "list-keychains":
                    raise credentials.CredentialError
                return b""

            with (
                patch.dict(os.environ, {"RUNNER_TEMP": str(root)}, clear=True),
                patch.object(credentials, "run", side_effect=run),
                self.assertRaises(credentials.CredentialError),
            ):
                credentials.cleanup()
            self.assertTrue(
                any(command[1] == "delete-keychain" for command in commands)
            )
            self.assertFalse(managed.exists())

    def test_cleanup_rejects_unmanaged_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = root / "diskie-signing"
            managed.mkdir()
            marker = managed / "keep"
            marker.write_text("unrelated")
            with (
                patch.dict(os.environ, {"RUNNER_TEMP": str(root)}, clear=True),
                self.assertRaises((OSError, credentials.CredentialError)),
            ):
                credentials.cleanup()
            self.assertTrue(marker.exists())

    def test_subprocess_diagnostics_and_credential_environment_are_not_exposed(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with (
                patch.dict(os.environ, environment(Path(temporary)), clear=True),
                patch.object(
                    credentials.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], 1, b"synthetic secret", b"synthetic secret"
                    ),
                ) as command,
                contextlib.redirect_stderr(output),
            ):
                self.assertEqual(credentials.main(["setup"]), 1)
            self.assertEqual(output.getvalue(), "apple_credentials_setup_failed\n")
            self.assertTrue(
                set(credentials.REQUIRED).isdisjoint(command.call_args.kwargs["env"])
            )


if __name__ == "__main__":
    unittest.main()
