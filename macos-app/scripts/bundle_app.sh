#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SUPERVISOR_ROOT="$(cd "$APP_SRC_DIR/.." && pwd)"
DIST_DIR="$SUPERVISOR_ROOT/dist"
APP_BUNDLE="$DIST_DIR/OpsSupervisor.app"

echo "==> [1/4] Building OpsSupervisor release binary via SwiftPM..."
cd "$APP_SRC_DIR"
swift build -c release

BIN_PATH="$(swift build -c release --show-bin-path)/OpsSupervisor"
if [[ ! -x "$BIN_PATH" ]]; then
    echo "Error: Built binary not found at $BIN_PATH" >&2
    exit 1
fi

echo "==> [2/4] Generating vector macOS AppIcon.icns..."
WORK_ICON_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_ICON_DIR"' EXIT
ICONSET_DIR="$WORK_ICON_DIR/AppIcon.iconset"
swift "$SCRIPT_DIR/generate_icon.swift" "$ICONSET_DIR"

mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources/cli"

iconutil -c icns "$ICONSET_DIR" -o "$APP_BUNDLE/Contents/Resources/AppIcon.icns"

echo "==> [3/4] Assembling OpsSupervisor.app bundle..."
cp "$BIN_PATH" "$APP_BUNDLE/Contents/MacOS/OpsSupervisor"
chmod +x "$APP_BUNDLE/Contents/MacOS/OpsSupervisor"

# Bundle embedded CLI, Scheduler, Bench-Specs, Breakthrough Playbook and Bridge protocol
# inside Contents/Resources/ (bridge runtime state — inbox/outbox/ledger — is excluded).
for subdir in cli scheduler bench-specs breakthrough bridge; do
    rm -rf "$APP_BUNDLE/Contents/Resources/$subdir"
    if [[ -d "$SUPERVISOR_ROOT/$subdir" ]]; then
        mkdir -p "$APP_BUNDLE/Contents/Resources/$subdir"
        rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.state' \
              --exclude='ledger.jsonl' --exclude='inbox' --exclude='outbox' --exclude='*.lock' \
              "$SUPERVISOR_ROOT/$subdir/" "$APP_BUNDLE/Contents/Resources/$subdir/"
        mkdir -p "$APP_BUNDLE/Contents/Resources/$subdir/inbox" "$APP_BUNDLE/Contents/Resources/$subdir/outbox"
    fi
done
chmod +x "$APP_BUNDLE/Contents/Resources/cli/opsb" "$APP_BUNDLE/Contents/Resources/cli/opsb_core/probe/npu_probe.py"
mkdir -p "$HOME/.local/bin"
ln -sf "$SUPERVISOR_ROOT/cli/opsb" "$HOME/.local/bin/opsb"

cat > "$APP_BUNDLE/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleDisplayName</key>
    <string>OpsSupervisor</string>
    <key>CFBundleExecutable</key>
    <string>OpsSupervisor</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>dev.winbeau.OpsSupervisor</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>OpsSupervisor</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
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

echo "==> [4/4] Ad-hoc signing OpsSupervisor.app..."
codesign --force --deep --sign - "$APP_BUNDLE"

echo "==> Done! Bundle ready at: $APP_BUNDLE"

if [[ "${1:-}" == "--install" || "${INSTALL_TO_APPLICATIONS:-0}" == "1" ]]; then
    echo "==> [install] Copying bundle to /Applications and registering with LaunchServices..."
    TARGET="/Applications/OpsSupervisor.app"
    if pgrep -f "/Applications/OpsSupervisor.app/Contents/MacOS/OpsSupervisor" >/dev/null 2>&1; then
        echo "    quit the installed app first (it is currently running) — skipping live copy" >&2
    else
        rm -rf "$TARGET"
        ditto "$APP_BUNDLE" "$TARGET"
        xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null || true
        codesign --force --deep -s - "$TARGET" >/dev/null 2>&1 || true
        /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$TARGET" || true
        echo "    installed: $TARGET (bundle id dev.winbeau.OpsSupervisor)"
    fi
fi
