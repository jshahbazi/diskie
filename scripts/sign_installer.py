#!/usr/bin/env python3
"""Sign, notarize and verify Diskie's source-bound macOS installer.

The signing boundaries follow Stark's standalone release recipe. Credentials
remain in a temporary Keychain; only fixed status, hashes and receipts leave
this command. See docs/releasing.md for provenance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / '.build/Diskie.app'
BUNDLE_ID = 'local.diskspace.explorer'
BINARIES = ('Contents/Resources/disk-scanner', 'Contents/Resources/diskie-update-helper', 'Contents/MacOS/Diskie')
SHA = re.compile(r'[0-9a-f]{40}\Z')
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')


class ReleaseError(Exception):
    pass


def require(condition, stage):
    if not condition:
        raise ReleaseError(stage)


def environment():
    result = {name: os.environ[name] for name in ('HOME', 'TMPDIR') if name in os.environ}
    result.update(PATH='/usr/bin:/bin:/usr/sbin:/sbin', LANG='C', LC_ALL='C')
    return result


def run(arguments, stage, timeout=120):
    try:
        result = subprocess.run([str(x) for x in arguments], cwd=ROOT,
                                env=environment(), stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError(stage) from None
    require(result.returncode == 0, stage)
    return result


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(app):
    files = {}
    for path in sorted(app.rglob('*')):
        require(not path.is_symlink(), 'application_symlink_rejected')
        if path.is_file():
            files[path.relative_to(app).as_posix()] = digest(path)
        else:
            require(path.is_dir(), 'application_special_file_rejected')
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def resolve_identity(selected, keychain):
    result = run(['/usr/bin/security', 'find-identity', '-v', '-p', 'codesigning', keychain], 'identity_lookup')
    matches = []
    for line in result.stdout.decode().splitlines():
        match = re.fullmatch(r'\s*\d+\) ([0-9A-Fa-f]{40}) "(Developer ID Application: .+ \(([A-Z0-9]{10})\))"\s*', line)
        if match and selected in (match[1], match[1].lower(), match[2]):
            matches.append({'sha1': match[1].upper(), 'team_id': match[3]})
    require(len(matches) == 1, 'developer_id_identity_required')
    return matches[0]


def verify_signature(path, identity, runtime=True):
    require(re.fullmatch(r'[0-9A-F]{40}', identity['sha1']) is not None, 'certificate_receipt_invalid')
    require(re.fullmatch(r'[A-Z0-9]{10}', identity['team_id']) is not None, 'team_receipt_invalid')
    requirement = ('=anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists '
                   f'and certificate leaf = H"{identity["sha1"]}"')
    run(['/usr/bin/codesign', '--verify', '--strict', '-R', requirement, path], 'signature_invalid')
    detail = run(['/usr/bin/codesign', '--display', '--verbose=4', path], 'signature_metadata').stderr.decode(errors='replace')
    require(f'TeamIdentifier={identity["team_id"]}' in detail, 'signing_team_mismatch')
    timestamp = re.search(r'(?m)^Timestamp=(.+)$', detail)
    require(timestamp and timestamp[1].strip().lower() not in ('none', 'not set'), 'secure_timestamp_missing')
    if runtime:
        flags = re.search(r'(?m)^CodeDirectory\b[^\n]*\bflags=0x([0-9a-fA-F]+)\(', detail)
        require(flags and int(flags[1], 16) & 0x10000, 'hardened_runtime_missing')
        entitlements = run(['/usr/bin/codesign', '--display', '--entitlements', ':-', path], 'entitlements_read')
        require(b'<key>' not in entitlements.stdout + entitlements.stderr, 'unexpected_entitlements')


def notarize(path, keychain, profile):
    result = run(['/usr/bin/xcrun', 'notarytool', 'submit', path, '--keychain-profile', profile,
                  '--keychain', keychain, '--wait', '--timeout', '30m', '--output-format', 'json'],
                 'notarization_submission_failed', timeout=1860)
    try:
        data = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise ReleaseError('notarization_receipt_invalid') from None
    require(isinstance(data, dict), 'notarization_receipt_invalid')
    require(data.get('status') == 'Accepted', 'notarization_not_accepted')
    require(isinstance(data.get('id'), str) and re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', data['id']),
            'notarization_receipt_invalid')
    return {'status': 'Accepted', 'submission_id': data['id']}


def validate_app(app, source, version, identity):
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    require(info.get('CFBundleIdentifier') == BUNDLE_ID and info.get('CFBundleShortVersionString') == version,
            'application_identity_mismatch')
    build = json.loads((app / 'Contents/Resources/build-info.json').read_bytes())
    require(build == {'source_revision': source, 'version': version}, 'application_source_mismatch')
    for name in BINARIES:
        verify_signature(app / name, identity)
    verify_signature(app, identity)
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app], 'application_seal_invalid')
    run(['/usr/bin/xcrun', 'stapler', 'validate', app], 'application_ticket_invalid')
    run(['/usr/sbin/spctl', '--assess', '--type', 'execute', app], 'application_gatekeeper_rejected')


def verify_installer(image, receipt, expected_source):
    require(receipt.get('source_revision') == expected_source and SHA.fullmatch(expected_source), 'source_receipt_mismatch')
    require(receipt.get('version') and VERSION.fullmatch(receipt['version']), 'version_receipt_invalid')
    require(digest(image) == receipt.get('dmg_sha256'), 'installer_digest_mismatch')
    identity = receipt['identity']
    verify_signature(image, identity, runtime=False)
    run(['/usr/bin/hdiutil', 'verify', image], 'disk_image_integrity_invalid')
    run(['/usr/bin/xcrun', 'stapler', 'validate', image], 'disk_image_ticket_invalid')
    run(['/usr/sbin/spctl', '--assess', '--type', 'open', '--context', 'context:primary-signature', image],
        'disk_image_gatekeeper_rejected')
    with tempfile.TemporaryDirectory(prefix='diskie-verify-') as temporary:
        mount = Path(temporary) / 'mounted'
        mount.mkdir()
        attached = False
        try:
            run(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse', '-noautoopen', '-mountpoint', mount, image], 'disk_image_mount_failed')
            attached = True
            link = mount / 'Applications'
            require(link.is_symlink() and link.readlink() == Path('/Applications'), 'applications_link_invalid')
            app = mount / 'Diskie.app'
            require(app.is_dir() and not app.is_symlink(), 'application_missing')
            require(inventory(app) == receipt['application_inventory_sha256'], 'application_bytes_changed')
            validate_app(app, expected_source, receipt['version'], identity)
        finally:
            if attached or mount.is_mount():
                try:
                    run(['/usr/bin/hdiutil', 'detach', mount], 'disk_image_detach_failed')
                except ReleaseError:
                    run(['/usr/bin/hdiutil', 'detach', '-force', mount], 'disk_image_force_detach_failed')


def sign_installer(options):
    require(platform.machine() == 'arm64', 'apple_silicon_runner_required')
    require(SHA.fullmatch(options.source), 'source_revision_invalid')
    revision = run(['/usr/bin/git', 'rev-parse', 'HEAD'], 'source_revision_read').stdout.decode().strip()
    require(revision == options.source, 'source_revision_mismatch')
    require(not run(['/usr/bin/git', 'status', '--porcelain', '--untracked-files=all'], 'source_status').stdout, 'source_tree_dirty')
    identity = resolve_identity(options.identity, options.keychain)
    info = plistlib.loads((APP / 'Contents/Info.plist').read_bytes())
    version = info.get('CFBundleShortVersionString', '')
    require(VERSION.fullmatch(version), 'version_invalid')
    require(info.get('CFBundleIdentifier') == BUNDLE_ID, 'bundle_id_mismatch')
    resources = APP / 'Contents/Resources'
    require({p.name for p in resources.iterdir()} == {'chart.html', 'icon.icns', 'disk-scanner', 'diskie-update-helper', 'Stark-MIT.md'}, 'unexpected_application_resources')
    (resources / 'build-info.json').write_text(json.dumps({'source_revision': options.source, 'version': version}) + '\n')
    for path in [*(APP / name for name in BINARIES), APP]:
        if path != APP:
            arches = run(['/usr/bin/lipo', '-archs', path], 'architecture_read').stdout.decode().split()
            require(arches == ['arm64'], 'application_architecture_mismatch')
        run(['/usr/bin/codesign', '--force', '--sign', identity['sha1'], '--keychain', options.keychain,
             '--options', 'runtime', '--timestamp', path], 'application_signing_failed')
        verify_signature(path, identity)
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    image = output / f'diskie-{version}-aarch64-apple-darwin.dmg'
    require(not image.exists(), 'installer_already_exists')
    with tempfile.TemporaryDirectory(prefix='diskie-notarize-') as temporary:
        archive = Path(temporary) / 'Diskie.zip'
        run(['/usr/bin/ditto', '-c', '-k', '--keepParent', APP, archive], 'application_archive_failed')
        app_notary = notarize(archive, options.keychain, options.profile)
    run(['/usr/bin/xcrun', 'stapler', 'staple', APP], 'application_stapling_failed')
    validate_app(APP, options.source, version, identity)
    app_digest = inventory(APP)
    run(['/bin/bash', ROOT / 'scripts/package.sh'], 'disk_image_packaging_failed', timeout=600)
    shutil.copyfile(ROOT / 'dist/Diskie.dmg', image)
    run(['/usr/bin/codesign', '--force', '--sign', identity['sha1'], '--keychain', options.keychain,
         '--timestamp', '--identifier', BUNDLE_ID + '.diskimage', image], 'disk_image_signing_failed')
    image_notary = notarize(image, options.keychain, options.profile)
    run(['/usr/bin/xcrun', 'stapler', 'staple', image], 'disk_image_stapling_failed')
    receipt = {'schema_version': 1, 'version': version, 'source_revision': options.source,
               'identity': identity, 'application_inventory_sha256': app_digest,
               'dmg_sha256': digest(image), 'application_notarization': app_notary,
               'disk_image_notarization': image_notary, 'application_ticket': 'stapled', 'disk_image_ticket': 'stapled'}
    verify_installer(image, receipt, options.source)
    alias = output / 'Diskie.dmg'
    require(not alias.exists(), 'installer_alias_already_exists')
    shutil.copyfile(image, alias)
    for path in (image, alias):
        path.with_suffix(path.suffix + '.sha256').write_text(f'{receipt["dmg_sha256"]}  {path.name}\n')
    (output / 'Diskie.dmg.notarization.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/signed-installer')
    parser.add_argument('--identity', default=os.environ.get('MACOS_SIGNING_IDENTITY', ''))
    parser.add_argument('--keychain', type=Path)
    parser.add_argument('--profile', default='diskie-notary')
    parser.add_argument('--verify-only', action='store_true')
    options = parser.parse_args(arguments)
    try:
        if options.verify_only:
            receipt = json.loads((options.output / 'Diskie.dmg.notarization.json').read_bytes())
            verify_installer(options.output / 'Diskie.dmg', receipt, options.source)
        else:
            require(options.identity and options.keychain, 'signing_configuration_missing')
            sign_installer(options)
    except ReleaseError as error:
        print(f'diskie_installer_failed:{error}', file=sys.stderr)
        return 1
    except Exception:
        print('diskie_installer_failed:invalid_release_data', file=sys.stderr)
        return 1
    print('diskie_installer_verified')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
