#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$REPO_ROOT/dist"
APP_BUNDLE="$DIST_DIR/Parhelion.app"

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
    VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP_BUNDLE/Contents/Info.plist" 2>/dev/null || echo '2.0.0')"
fi
ARCH="$(uname -m)"
DMG_NAME="Parhelion-v${VERSION}-${ARCH}.dmg"
DMG_PATH="$DIST_DIR/$DMG_NAME"
SHA_PATH="${DMG_PATH}.sha256"

if [[ ! -d "$APP_BUNDLE" || "${REBUILD_APP:-0}" == "1" ]]; then
    echo "==> Building Parhelion.app before DMG packaging..."
    "$SCRIPT_DIR/bundle_app.sh"
fi

echo "==> Packaging $DMG_NAME..."
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

ditto "$APP_BUNDLE" "$STAGE_DIR/Parhelion.app"
ln -s /Applications "$STAGE_DIR/Applications"

rm -f "$DMG_PATH" "$SHA_PATH"
hdiutil create \
    -volname "Parhelion ${VERSION}" \
    -srcfolder "$STAGE_DIR" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=9 \
    "$DMG_PATH"

(
    cd "$DIST_DIR"
    shasum -a 256 "$DMG_NAME" > "$SHA_PATH"
)

echo "==> DMG ready: $DMG_PATH ($(du -h "$DMG_PATH" | awk '{print $1}'))"
cat "$SHA_PATH"
