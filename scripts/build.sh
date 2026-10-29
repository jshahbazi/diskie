#!/bin/bash
set -euo pipefail
source_dir="$(cd "$(dirname "$0")/.." && pwd)"
build_dir="$source_dir/.build"
app_dir="$build_dir/Diskie.app"
signing_identity="${SIGNING_IDENTITY:--}"
build_arch="${BUILD_ARCH:-$(uname -m)}"
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources"
clang++ -std=c++17 -O3 -Wall -Wextra -pthread -target "$build_arch-apple-macos13.0" "$source_dir/src/scanner.cpp" -o "$build_dir/scanner-new"
swiftc -O -swift-version 5 -target "$build_arch-apple-macos13.0" "$source_dir/src/main.swift" -o "$build_dir/app-new" -framework Cocoa -framework WebKit
cp "$build_dir/scanner-new" "$app_dir/Contents/Resources/disk-scanner"
cp "$build_dir/app-new" "$app_dir/Contents/MacOS/Diskie"
cp "$source_dir/Resources/Info.plist" "$app_dir/Contents/Info.plist"
if [[ -n "${DISKIE_RELEASE_VERSION:-}" ]]; then
  if ! [[ "$DISKIE_RELEASE_VERSION" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
    echo 'Invalid DISKIE_RELEASE_VERSION.' >&2
    exit 1
  fi
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $DISKIE_RELEASE_VERSION" "$app_dir/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $DISKIE_RELEASE_VERSION" "$app_dir/Contents/Info.plist"
fi
cp "$source_dir/Resources/chart.html" "$source_dir/Resources/icon.icns" "$app_dir/Contents/Resources/"
codesign --force --sign "$signing_identity" "$app_dir/Contents/Resources/disk-scanner"
codesign --force --sign "$signing_identity" "$app_dir"
codesign --verify --deep --strict "$app_dir"
printf 'Built %s\n' "$app_dir"
