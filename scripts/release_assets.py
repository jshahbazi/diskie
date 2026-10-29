"""Validate the signed job's immutable assets before publishing a GitHub release."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')
SOURCE = re.compile(r'[0-9a-f]{40}\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def validate_assets(root: Path, source: str, version: str) -> dict:
    require(SOURCE.fullmatch(source), 'invalid_source_revision')
    require(VERSION.fullmatch(version), 'invalid_release_version')
    versioned = f'diskie-{version}-aarch64-apple-darwin.dmg'
    images = (versioned, 'Diskie.dmg')
    expected = {versioned, versioned + '.sha256', 'Diskie.dmg', 'Diskie.dmg.sha256', 'Diskie.dmg.notarization.json'}
    require(root.is_dir() and not root.is_symlink(), 'invalid_assets_directory')
    require({p.name for p in root.iterdir()} == expected, 'unexpected_release_assets')
    require(all(p.is_file() and not p.is_symlink() for p in root.iterdir()), 'invalid_release_asset_type')
    receipt = json.loads((root / 'Diskie.dmg.notarization.json').read_bytes())
    require(receipt.get('schema_version') == 1, 'invalid_receipt_schema')
    require(receipt.get('source_revision') == source, 'receipt_source_mismatch')
    require(receipt.get('version') == version, 'receipt_version_mismatch')
    require(DIGEST.fullmatch(receipt.get('dmg_sha256', '')), 'invalid_installer_digest')
    require(DIGEST.fullmatch(receipt.get('application_inventory_sha256', '')), 'invalid_application_inventory_digest')
    identity = receipt.get('identity', {})
    require(re.fullmatch(r'[0-9A-F]{40}', identity.get('sha1', '')), 'invalid_signing_certificate')
    require(re.fullmatch(r'[A-Z0-9]{10}', identity.get('team_id', '')), 'invalid_signing_team')
    for name in ('application_notarization', 'disk_image_notarization'):
        notarization = receipt.get(name, {})
        require(notarization.get('status') == 'Accepted', 'notarization_not_accepted')
        require(re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', notarization.get('submission_id', '')),
                'invalid_notarization_submission')
    require(receipt.get('application_ticket') == receipt.get('disk_image_ticket') == 'stapled', 'notarization_ticket_missing')
    for name in images:
        require(digest(root / name) == receipt['dmg_sha256'], 'release_asset_digest_mismatch')
        expected_checksum = f'{receipt["dmg_sha256"]}  {name}\n'
        require((root / (name + '.sha256')).read_text() == expected_checksum, 'release_checksum_file_mismatch')
    return receipt


def release_notes(source: str, version: str) -> str:
    return f'''Diskie {version} for Apple silicon, macOS 13 or newer.

Download **Diskie.dmg**, open it, and drag Diskie into Applications.

The application and installer are Developer ID signed with secure timestamps. Apple accepted both for notarization, and both tickets are stapled. Signatures, Gatekeeper acceptance, checksums, and the embedded application's file inventory were verified before publication.

Diskie scans the internal data volume locally. macOS may ask for access to protected folders; Full Disk Access is configured in System Settings → Privacy & Security.

Source commit: [{source}](https://github.com/jshahbazi/diskie/commit/{source}). SHA-256 checksums and the source-bound notarization receipt are attached.
'''


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--notes', type=Path, required=True)
    options = parser.parse_args(arguments)
    try:
        validate_assets(options.assets, options.source, options.version)
        options.notes.write_text(release_notes(options.source, options.version))
    except (OSError, ValueError, TypeError, AttributeError):
        print('Unable to validate signed release assets.', file=sys.stderr)
        return 1
    print(f'Validated signed release assets for v{options.version}.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
