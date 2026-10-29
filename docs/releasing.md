# Building installers in GitHub Actions

Every push and pull request runs **Build and test**, then uploads a `Diskie-development-COMMIT-ATTEMPT` artifact containing `Diskie.dmg` and its SHA-256 checksum. This is an ad hoc signed development build and has not been notarized.

## Signed installer

Run **Actions → Signed installer → Run workflow** on `main` and choose a `major`, `minor`, or `patch` bump (default: `patch`), or:

```sh
gh workflow run installer.yml --ref main -f bump=patch
```

The workflow builds the exact selected commit on an Apple silicon macOS 26 runner and runs the scanner, chart, and release-tool tests before importing credentials. It signs both native binaries and the application with Developer ID, hardened runtime, and secure timestamps. Apple must accept the application and DMG for notarization. The workflow staples both tickets and verifies the image by mounting it read-only. A second verification happens after credential cleanup.

The `Diskie-signed-COMMIT-ATTEMPT` artifact contains:

- A versioned `diskie-VERSION-aarch64-apple-darwin.dmg`.
- A byte-identical `Diskie.dmg` alias.
- SHA-256 checksum files for both names.
- `Diskie.dmg.notarization.json`, recording the source revision, version, signer, file-inventory digest, Apple acceptance identifiers, and final image hash.

The version selector uses the highest stable numeric `vMAJOR.MINOR.PATCH` Git tag or the `Resources/Info.plist` version as a lower bound, then applies the selected bump. Prerelease tags are ignored. The chosen version is applied to the built app without changing the source plist. For example, a `1.2.1` baseline produces `1.2.2` with a patch bump.

After the signed job succeeds, a separate publishing job downloads that exact job's immutable artifact ID. It validates the source revision, version, notarization receipts, both checksum files, and equality of the stable and versioned images. Only then does it create `vVERSION` at the dispatched source commit and publish a new latest GitHub release with all five assets. The publishing job has repository write permission and no Apple credentials. Existing releases, tags, and versioned assets are never replaced. If publication fails after tag creation, rerunning the failed publishing job can finish that version only when its tag points to the identical workflow source SHA and no release exists. A fresh full dispatch selects the next version instead.

The stable download URL is:

```text
https://github.com/jshahbazi/diskie/releases/latest/download/Diskie.dmg
```

The workflow artifact is also retained for 30 days. The workflow never starts the application or scans the runner's disk.

## Credentials and branch policy

The manual signing job uses the `macos-release` environment, restricted to `main`. Configure these six environment secrets using the same Apple credentials as the Stark release workflow:

- `MACOS_CERTIFICATE_P12_BASE64`
- `MACOS_CERTIFICATE_PASSWORD`
- `MACOS_SIGNING_IDENTITY`
- `APPLE_API_KEY_P8_BASE64`
- `APPLE_API_KEY_ID`
- `APPLE_API_ISSUER_ID`

GitHub secrets are scoped to their repository or organization; a separate repository cannot read Stark's environment-secret values. Provision Diskie's environment explicitly. Credential files and values must stay outside Git and logs.

Only the credential-import step receives all six secrets. The helper uses private runner files and a temporary Keychain, removes the certificate/key files, and restores the original Keychain search list during unconditional cleanup. Subsequent steps use the Keychain profile. The verification step needs no Apple credentials, and the build/signing job has read-only repository permissions. Only the separate publishing job receives `contents: write`.

A local signed image can be verified against a known source commit without importing credentials:

```sh
python3 scripts/sign_installer.py --verify-only --source SOURCE_COMMIT \
  --output /path/to/downloaded/artifact
```

## Reused Stark implementation

The credential helper and its recovery tests were adapted from [Stark commit `03bd056c34217147c436b47105c65c8a87c152be`](https://github.com/jshahbazi/stark/commit/03bd056c34217147c436b47105c65c8a87c152be), retaining the six secret names and output-redaction/cleanup behavior. The workspace, marker and notary profile are specific to Diskie.

The version selector follows Stark’s immutable-tag version-bumping pattern. The signing, notarization, mounted-image verification and workflow isolation follow Stark's standalone `signed-release.yml`, `scripts/sign_distribution.py`, and `scripts/build_dmg.py` recipe. Diskie's implementation targets its two-binary application and drag-to-Applications DMG. It does not include Stark's CLI/runtime distribution or replacement launcher. The [Stark MIT notice](third-party/stark-MIT.md) is retained for adapted code.
