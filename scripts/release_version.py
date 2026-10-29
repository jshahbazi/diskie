"""Select a Diskie release version from its plist baseline and fetched stable tags."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def parse_version(value: str) -> tuple[int, int, int] | None:
    if VERSION.fullmatch(value) is None:
        return None
    return tuple(map(int, value.split(".")))


def next_version(baseline: str, tags: list[str], bump: str) -> str:
    if bump not in ("major", "minor", "patch"):
        raise ValueError("invalid version bump")
    highest = parse_version(baseline)
    if highest is None:
        raise ValueError("invalid source version")
    for tag in tags:
        if tag.startswith("v"):
            candidate = parse_version(tag[1:])
            if candidate is not None:
                highest = max(highest, candidate)
    major, minor, patch = highest
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def current_baseline() -> str:
    with (ROOT / "Resources/Info.plist").open("rb") as source:
        info = plistlib.load(source)
    version = info["CFBundleShortVersionString"]
    if not isinstance(version, str) or parse_version(version) is None:
        raise ValueError("invalid plist version")
    return version


def verify_workflow_ref() -> None:
    ref = os.environ.get("GITHUB_REF")
    if ref is not None and ref != "refs/heads/main":
        raise ValueError("release selection must run from main")
    expected_sha = os.environ.get("GITHUB_SHA")
    if expected_sha is not None:
        actual_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if actual_sha != expected_sha:
            raise ValueError("checkout does not match workflow SHA")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bump", choices=("major", "minor", "patch"), default="patch")
    options = parser.parse_args()
    try:
        verify_workflow_ref()
        baseline = current_baseline()
        tags = subprocess.run(
            ["git", "tag", "--list", "v*"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        stable_tags = [tag for tag in tags if tag.startswith("v") and parse_version(tag[1:]) is not None]
        highest = max(
            [parse_version(baseline), *(parse_version(tag[1:]) for tag in stable_tags)]
        )
        version = next_version(baseline, tags, options.bump)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"version={version}\n")
    except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException, subprocess.CalledProcessError):
        parser.exit(1, "Unable to select release version.\n")
    print(
        f"Selected release version: {version} "
        f"(plist baseline {baseline}; highest stable tag v{'.'.join(map(str, highest))})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
