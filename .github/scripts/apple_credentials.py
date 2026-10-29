#!/usr/bin/env python3
"""Manage the temporary Apple credentials used by Diskie's signing job.

Credentials stay in the runner's private files, process memory and temporary
Keychain. Command output and exception details must never reach the job log.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

REQUIRED = (
    "MACOS_CERTIFICATE_P12_BASE64",
    "MACOS_CERTIFICATE_PASSWORD",
    "MACOS_SIGNING_IDENTITY",
    "APPLE_API_KEY_P8_BASE64",
    "APPLE_API_KEY_ID",
    "APPLE_API_ISSUER_ID",
)
PROFILE = "diskie-notary"
MARKER = ".diskie-credentials"
SEARCH_LIST = "original-keychains.json"


class CredentialError(Exception):
    """A failure whose details must remain outside public output."""


def run(*arguments: str) -> bytes:
    environment = {
        key: value for key, value in os.environ.items() if key not in REQUIRED
    }
    try:
        result = subprocess.run(
            arguments,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise CredentialError from None
    if result.returncode:
        raise CredentialError
    return result.stdout


def workspace() -> Path:
    temporary = os.environ.get("RUNNER_TEMP", "")
    if not temporary or not Path(temporary).is_absolute():
        raise CredentialError
    return Path(temporary) / "diskie-signing"


def write_private(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(value)


def read_search_list(root: Path) -> list[str]:
    original = json.loads((root / SEARCH_LIST).read_bytes())
    if not isinstance(original, list) or any(
        not isinstance(path, str) or not Path(path).is_absolute() for path in original
    ):
        raise CredentialError
    return original


def preflight(keychain: Path, identity: str) -> None:
    source = keychain.parent / "signing-probe.c"
    binary = keychain.parent / "signing-probe"
    try:
        write_private(source, b"int main(void) { return 0; }\n")
        run("/usr/bin/xcrun", "clang", str(source), "-o", str(binary))
        run(
            "/usr/bin/codesign",
            "--force",
            "--sign",
            identity,
            "--keychain",
            str(keychain),
            "--options",
            "runtime",
            "--timestamp=none",
            str(binary),
        )
        run("/usr/bin/codesign", "--verify", "--strict", str(binary))
    finally:
        source.unlink(missing_ok=True)
        binary.unlink(missing_ok=True)


def setup() -> None:
    if any(not os.environ.get(name) for name in REQUIRED):
        raise CredentialError
    identity = os.environ["MACOS_SIGNING_IDENTITY"]
    if not identity.startswith("Developer ID Application:") or any(
        character in identity for character in ("\0", "\n", "\r")
    ):
        raise CredentialError
    try:
        certificate_bytes = base64.b64decode(
            os.environ["MACOS_CERTIFICATE_P12_BASE64"], validate=True
        )
        key_bytes = base64.b64decode(
            os.environ["APPLE_API_KEY_P8_BASE64"], validate=True
        )
    except (ValueError, binascii.Error):
        raise CredentialError from None
    if not certificate_bytes or not key_bytes:
        raise CredentialError

    root = workspace()
    root.mkdir(mode=0o700)
    write_private(root / MARKER, b"diskie-signing-v1\n")
    certificate = root / "certificate.p12"
    api_key = root / "AuthKey.p8"
    keychain = root / "signing.keychain-db"
    password = secrets.token_urlsafe(48)
    try:
        original = shlex.split(
            run("/usr/bin/security", "list-keychains", "-d", "user").decode()
        )
        write_private(root / SEARCH_LIST, json.dumps(original).encode())
        # Validate saved recovery state before any Keychain mutation.
        read_search_list(root)
        write_private(certificate, certificate_bytes)
        write_private(api_key, key_bytes)
        run("/usr/bin/security", "create-keychain", "-p", password, str(keychain))
        run("/usr/bin/security", "set-keychain-settings", "-lut", "3600", str(keychain))
        run("/usr/bin/security", "unlock-keychain", "-p", password, str(keychain))
        run(
            "/usr/bin/security",
            "import",
            str(certificate),
            "-k",
            str(keychain),
            "-P",
            os.environ["MACOS_CERTIFICATE_PASSWORD"],
            "-T",
            "/usr/bin/codesign",
        )
        run(
            "/usr/bin/security",
            "set-key-partition-list",
            "-S",
            "apple-tool:,apple:,codesign:",
            "-s",
            "-k",
            password,
            str(keychain),
        )
        run(
            "/usr/bin/security",
            "list-keychains",
            "-d",
            "user",
            "-s",
            str(keychain),
            *original,
        )
        preflight(keychain, identity)
        run(
            "/usr/bin/xcrun",
            "notarytool",
            "store-credentials",
            PROFILE,
            "--key",
            str(api_key),
            "--key-id",
            os.environ["APPLE_API_KEY_ID"],
            "--issuer",
            os.environ["APPLE_API_ISSUER_ID"],
            "--keychain",
            str(keychain),
        )
    finally:
        certificate.unlink(missing_ok=True)
        api_key.unlink(missing_ok=True)


def cleanup() -> None:
    root = workspace()
    if not root.exists():
        return
    if root.is_symlink() or (root / MARKER).read_bytes() != b"diskie-signing-v1\n":
        raise CredentialError
    keychain = root / "signing.keychain-db"
    failed = False
    try:
        # Restoration and Keychain deletion are independent recovery actions.
        # Attempt both even when a preceding command failed.
        if (root / SEARCH_LIST).exists():
            try:
                original = read_search_list(root)
                run(
                    "/usr/bin/security", "list-keychains", "-d", "user", "-s", *original
                )
            except (OSError, ValueError, CredentialError):
                failed = True
        if keychain.exists():
            try:
                run("/usr/bin/security", "delete-keychain", str(keychain))
            except CredentialError:
                failed = True
    finally:
        shutil.rmtree(root)
    if failed:
        raise CredentialError


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "cleanup"))
    options = parser.parse_args(arguments)
    try:
        if options.action == "setup":
            try:
                setup()
            except Exception:
                cleanup()
                raise
        else:
            cleanup()
    except Exception:  # noqa: BLE001 - never print commands, credentials or exceptions
        print(f"apple_credentials_{options.action}_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
