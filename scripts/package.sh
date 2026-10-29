#!/bin/bash
set -euo pipefail
source_dir="$(cd "$(dirname "$0")/.." && pwd)"
app_dir="$source_dir/.build/Diskie.app"
if [[ ! -d "$app_dir" ]]; then "$source_dir/scripts/build.sh"; fi
staging="$(mktemp -d)"
trap 'rm -r "$staging"' EXIT
ditto "$app_dir" "$staging/Diskie.app"
ln -s /Applications "$staging/Applications"
cp "$source_dir/README.md" "$staging/Read Me.md"
mkdir -p "$source_dir/dist"
hdiutil create -volname Diskie -srcfolder "$staging" -ov -format UDZO "$source_dir/dist/Diskie.dmg"
