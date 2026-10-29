# Diskie

A local macOS disk usage explorer with a drill-down chart. Scan the internal data volume, explore the largest folders and files, and rescan when you need a fresh view.

Diskie opens the last saved scan immediately. A native background scanner uses up to six workers and macOS bulk metadata reads. The toolbar shows the file count, elapsed time, and worker count; Cancel keeps the previous snapshot.

## Build and run

Requires macOS 13 or later and the Xcode command-line tools. Apple silicon has been tested. The build targets the host architecture.

```sh
./scripts/build.sh
open .build/Diskie.app
```

To install, copy `.build/Diskie.app` into Applications. To create a disk image:

```sh
./scripts/package.sh
```

The installer is written to `dist/Diskie.dmg`. A default build uses an ad hoc signature for local development. To use your own signing identity:

```sh
SIGNING_IDENTITY='Developer ID Application: Your Name (TEAMID)' ./scripts/build.sh
```

Use the same signing identity and bundle identifier for updates so macOS can recognize the app and retain its privacy approvals. Signing does not notarize a build.

## Folder access

macOS manages access to protected folders. Enable Diskie in **System Settings → Privacy & Security → Full Disk Access**, then quit and reopen the app. Approvals may need to be renewed when moving from an ad hoc build to a Developer ID build. Diskie cannot grant itself Full Disk Access.

Some system-managed paths can remain unreadable. The chart and toolbar report unreadable entries, and the previous snapshot remains available if you cancel or a scan fails.

## Reading the chart

Click a slice or row to drill down. Use the path at the top to go back. Files and folders under 100 MiB are grouped into **Other**; their bytes still contribute to the containing folder.

The disk overview shows the operating system's reported capacity. Folder sizes estimate allocated bytes. APFS clones, snapshots, compression, hard links, and protected paths can make folder totals differ from capacity or from the space a deletion would reclaim. Hard-linked files are counted once across workers; the first path encountered receives their bytes. Symlinks are not followed and other mounted volumes are excluded. Results are a snapshot of a changing filesystem.

Scans run locally and read folder metadata. The app has no network requests or deletion controls. New installations save scan data under `~/Library/Application Support/Diskie/`. Upgrades from the original Disk Space Explorer prototype keep its existing data directory and bundle identifier for compatibility.

## Tests

```sh
./scripts/build.sh
python3 -m unittest discover -s tests -v
node tests/test_chart.cjs
```

The scanner tests cover allocated-byte accounting, hard links, sparse files, Unicode filenames, symlink exclusion, permission failures, multiple workers, the bounded queue, and the POSIX fallback.

## Scanner CLI

```sh
.build/Diskie.app/Contents/Resources/disk-scanner OUTPUT [ROOT [WORKERS [posix]]]
```

`ROOT` defaults to `/System/Volumes/Data`. `WORKERS` accepts 1–16; the app uses up to six based on available CPU cores. The optional `posix` argument disables bulk reads for testing. Progress is written beside `OUTPUT` and removed on successful completion.

See [performance notes](docs/performance.md) for implementation details and measured results.
