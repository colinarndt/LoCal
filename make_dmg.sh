#!/bin/bash
# Build the app and wrap it in a drag-to-Applications disk image.
#
#     ./make_dmg.sh              -> dist/Instagram Calendar.dmg
#
# hdiutil rather than create-dmg: hdiutil ships with macOS, and the fancy part
# create-dmg adds (background art, positioned icons) is a .DS_Store baked by
# driving Finder over AppleScript, which needs a logged-in session and breaks
# under ssh or CI. Two icons and a symlink do not need that.
#
# The image is NOT notarized -- there is no Developer ID here. Gatekeeper will
# refuse the first launch on someone else's Mac; the README says how to get past
# it. That is the whole cost of not paying Apple, and it is a one-time click.
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="Instagram Calendar"
APP="dist/${APP_NAME}.app"
DMG="dist/${APP_NAME}.dmg"
STAGE="build/dmg"

if [ "${1:-}" != "--no-build" ]; then
    if [ -x ".venv/bin/python" ] && [ -x ".venv/bin/pyinstaller" ]; then
        CALENDAR_PYTHON=".venv/bin/python"
        CALENDAR_PYINSTALLER=".venv/bin/pyinstaller"
    else
        CALENDAR_PYTHON="$(command -v python3 || command -v python)"
        CALENDAR_PYINSTALLER="$(command -v pyinstaller || true)"
    fi
    [ -n "$CALENDAR_PYINSTALLER" ] || {
        echo "error: pyinstaller not found (install it or create .venv)" >&2
        exit 1
    }
    echo "==> Drawing AppIcon.icns"
    "$CALENDAR_PYTHON" make_icon.py
    echo "==> Building ${APP_NAME}.app"
    "$CALENDAR_PYINSTALLER" --noconfirm InstagramCalendar.spec
fi

[ -d "$APP" ] || { echo "error: $APP not found (run without --no-build)" >&2; exit 1; }

echo "==> Staging"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
# The drag target. A symlink, so it costs no space in the image and always
# points at the Applications folder of whoever mounts it.
ln -s /Applications "$STAGE/Applications"

# Nothing built locally is quarantined, but a stray com.apple.quarantine on the
# source tree would be copied into the image and then blamed on the download.
xattr -cr "$STAGE/${APP_NAME}.app" 2>/dev/null || true

echo "==> Creating $DMG"
hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$STAGE" \
    -ov \
    -format UDZO \
    -quiet \
    "$DMG"

rm -rf "$STAGE"
echo "==> $DMG ($(du -h "$DMG" | cut -f1))"
