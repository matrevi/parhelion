#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Build the bundle OUTSIDE the repository: anything under a Spotlight-indexed tree
# gets re-registered in LaunchServices by mdworker within seconds of every build,
# which makes the artifact compete with the installed /Applications copy for the
# same CFBundleIdentifier (and for parhelion:// links). $TMPDIR is not indexed.
BUILD_DIR="${PARHELION_BUILD_DIR:-${TMPDIR:-/tmp}/parhelion-build}"
APP_BUNDLE="$BUILD_DIR/Parhelion.app"
mkdir -p "$BUILD_DIR"

echo "==> [1/4] Building Parhelion release binary via SwiftPM..."
cd "$REPO_ROOT"
swift build -c release

BIN_PATH="$(swift build -c release --show-bin-path)/Parhelion"
if [[ ! -x "$BIN_PATH" ]]; then
    echo "Error: Built binary not found at $BIN_PATH" >&2
    exit 1
fi

echo "==> [2/4] Generating vector macOS AppIcon.icns..."
WORK_ICON_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_ICON_DIR"' EXIT
ICONSET_DIR="$WORK_ICON_DIR/AppIcon.iconset"
swift "$SCRIPT_DIR/generate_icon.swift" "$ICONSET_DIR"

rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

iconutil -c icns "$ICONSET_DIR" -o "$APP_BUNDLE/Contents/Resources/AppIcon.icns"

echo "==> [3/4] Assembling Parhelion.app bundle (zero contest data)..."
cp "$BIN_PATH" "$APP_BUNDLE/Contents/MacOS/Parhelion"
chmod +x "$APP_BUNDLE/Contents/MacOS/Parhelion"

# Bundle ONLY generic runtime code & templates inside Contents/Resources/.
# All user/contest data (bench-specs, breakthrough, state, config) lives strictly under ~/.parhelion/.
for subdir in cli scheduler runtime templates skills; do
    rm -rf "$APP_BUNDLE/Contents/Resources/$subdir"
    if [[ -d "$REPO_ROOT/$subdir" ]]; then
        mkdir -p "$APP_BUNDLE/Contents/Resources/$subdir"
        rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.state' \
              --exclude='*.lock' --exclude='.DS_Store' --exclude='tests' \
              "$REPO_ROOT/$subdir/" "$APP_BUNDLE/Contents/Resources/$subdir/"
    fi
done
chmod +x "$APP_BUNDLE/Contents/Resources/cli/opsb" \
         "$APP_BUNDLE/Contents/Resources/cli/opsb_core/probe/npu_probe.py" \
         "$APP_BUNDLE/Contents/Resources/cli/opsb_core/probe/npu_stream_probe.py"

mkdir -p "$HOME/.local/bin"
ln -sf "$REPO_ROOT/cli/opsb" "$HOME/.local/bin/opsb"

cat > "$APP_BUNDLE/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleDisplayName</key>
    <string>Parhelion</string>
    <key>CFBundleExecutable</key>
    <string>Parhelion</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>dev.winbeau.Parhelion</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>Parhelion</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>3.2.8</string>
    <key>CFBundleVersion</key>
    <string>18</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>LSUIElement</key>
    <false/>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSPrincipalClass</key>
    <string>NSApplication</string>
</dict>
</plist>
PLIST

echo "==> [4/4] Ad-hoc signing Parhelion.app..."
codesign --force --deep --sign - "$APP_BUNDLE"

# Belt and braces: even in $TMPDIR, make sure the fresh bundle is not left as a
# LaunchServices peer of the installed app (same CFBundleIdentifier).
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
"$LSREGISTER" -u "$APP_BUNDLE" >/dev/null 2>&1 || true

echo "==> Done! Bundle ready at: $APP_BUNDLE (outside the repo, LaunchServices registration pruned)"

if [[ "${1:-}" == "--install" || "${INSTALL_TO_APPLICATIONS:-0}" == "1" ]]; then
    echo "==> [install] Copying bundle to /Applications/Parhelion.app..."
    TARGET="/Applications/Parhelion.app"
    if pgrep -f "/Applications/Parhelion.app/Contents/MacOS/Parhelion" >/dev/null 2>&1; then
        echo "    quit Parhelion.app first (it is currently running) — skipping live copy" >&2
        exit 1
    fi
    rm -rf "$TARGET"
    ditto "$APP_BUNDLE" "$TARGET"
    xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null || true
    codesign --force --deep -s - "$TARGET" >/dev/null 2>&1 || true
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$TARGET" || true
    # Clean up legacy OpsSupervisor.app if no longer running
    if [[ -d "/Applications/OpsSupervisor.app" ]] && ! pgrep -f "/Applications/OpsSupervisor.app" >/dev/null 2>&1; then
        rm -rf "/Applications/OpsSupervisor.app"
        echo "    removed legacy /Applications/OpsSupervisor.app"
    fi
    echo "    installed: $TARGET (bundle id dev.winbeau.Parhelion)"
fi
