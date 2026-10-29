# Diskie

See what's taking up space on your Mac with an interactive, drill-down disk usage chart.

**[Download Diskie](https://github.com/jshahbazi/diskie/releases/latest/download/Diskie.dmg)** · Apple silicon · macOS 13 or later

## Install

Open the downloaded disk image, drag **Diskie** into **Applications**, and launch it. The app starts scanning automatically.

For access to protected folders, enable Diskie in **System Settings → Privacy & Security → Full Disk Access**, then quit and reopen it. Some system-managed paths may still be unreadable; Diskie reports those in the scan results.

## Explore your disk

- Click a chart slice or folder row to drill down. Use the path above the chart to go back.
- Diskie shows your last saved scan immediately and starts a fresh scan on every launch.
- Choose **Rescan** whenever you want a fresh view. **Cancel** stops the scan and keeps the previous result.
- Files and folders smaller than 100 MiB are grouped into **Other**, with their space included in the totals.

Diskie scans your Mac's internal data volume. Folder sizes estimate disk usage; APFS snapshots, shared files, and unreadable folders can make the totals differ from reported disk usage or the space a deletion would free.

## Updates

Diskie checks for updates on launch and every six hours while open. You can also choose **Check for Updates…** in the toolbar or Diskie menu.

When an update is available, use the toolbar's update button and choose **Install and Restart**. Diskie downloads and verifies the signed, notarized GitHub release, replaces itself, and reopens. Keep Diskie in `/Applications` or `~/Applications` to use in-app updates.

## Privacy

Diskie reads file and folder metadata and saves scan results on your Mac. The updater contacts GitHub to check for releases and download updates. Scan results and local file paths are never uploaded.

## Development

Requires the Xcode command-line tools.

```sh
./scripts/build.sh
open .build/Diskie.app
```

Local builds use an ad hoc signature. Use the published release for in-app updates.

Run the tests:

```sh
python3 -m unittest discover -s tests -v
node tests/test_chart.cjs
```

See [release builds](docs/releasing.md) and [scanner performance](docs/performance.md) for implementation details.
