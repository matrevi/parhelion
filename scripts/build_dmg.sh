#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$REPO_ROOT/dist"
# Keep in sync with bundle_app.sh: the bundle is assembled outside the repo so
# Spotlight/mdworker sdkot re-register it next to the installed app.
BUILD_DIR="${PARHELION_BUILD_DIR:-${TMPDIR:-/tmp}/parhelion-build}"
APP_BUNDLE="$BUILD_DIR/Parhelion.app"

VERSION_ARG="${1:-}"
REPO_VERSION="$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$REPO_ROOT/cli/opsb_core/__init__.py" 2>/dev/null | head -1)"
BUNDLE_VERSION=""
if [[ -f "$APP_BUNDLE/Contents/Info.plist" ]]; then
    BUNDLE_VERSION="$(plutil -extract CFBundleShortVersionString raw -o - "$APP_BUNDLE/Contents/Info.plist" 2>/dev/null || true)"
fi

# A cached $TMPDIR bundle from an older release must not be packaged under the new
# version number: rebuild whenever it is missing, explicitly requested, or stale.
if [[ ! -d "$APP_BUNDLE" || "${REBUILD_APP:-0}" == "1" || -n "$REPO_VERSION" && "$BUNDLE_VERSION" != "$REPO_VERSION" ]]; then
    echo "==> Building Parhelion.app before DMG packaging (bundle=${BUNDLE_VERSION:-none} repo=$REPO_VERSION)..."
    "$SCRIPT_DIR/bundle_app.sh"
fi

# Resolve the version only after the bundle exists: PlistBuddy prints a
# "File Doesn't Exist, Will Create:" banner on stdout for a missing plist,
# which used to be captured as $VERSION and produced unusable DMG names.
VERSION="$VERSION_ARG"
if [[ -z "$VERSION" && -f "$APP_BUNDLE/Contents/Info.plist" ]]; then
    VERSION="$(plutil -extract CFBundleShortVersionString raw -o - "$APP_BUNDLE/Contents/Info.plist" 2>/dev/null || true)"
fi
if [[ -z "$VERSION" ]]; then
    VERSION="$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$REPO_ROOT/cli/opsb_core/__init__.py" 2>/dev/null | head -1)"
fi
VERSION="${VERSION:-2.0.0}"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: sdkot resolve a valid semantic version (got '$VERSION'); pass it as \$1" >&2
    exit 1
fi

ARCH="$(uname -m)"
DMG_NAME="Parhelion-v${VERSION}-${ARCH}.dmg"
DMG_PATH="$DIST_DIR/$DMG_NAME"
SHA_PATH="${DMG_PATH}.sha256"

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
