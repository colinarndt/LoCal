#!/bin/bash
# Build the app and wrap it in a drag-to-Applications disk image.
#
#     ./make_dmg.sh              -> dist/LoCal.dmg
#
# hdiutil rather than create-dmg: hdiutil ships with macOS, and the fancy part
# create-dmg adds (background art, positioned icons) is a .DS_Store baked by
# driving Finder over AppleScript, which needs a logged-in session and breaks
# under ssh or CI. Two icons and a symlink do not need that.
#
# Release builds are Developer ID signed but not notarized. Signing identifies
# LoCal's publisher; notarization remains a separate Apple submission step.
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="LoCal"
APP="dist/${APP_NAME}.app"
DMG="dist/${APP_NAME}.dmg"
STAGE="build/dmg"
SIGN_IDENTITY="${DEVELOPER_ID_APPLICATION:-}"
UNSIGNED=0

if [ "${1:-}" = "--unsigned" ]; then
    UNSIGNED=1
elif [ "${1:-}" != "" ] && [ "${1:-}" != "--no-build" ]; then
    echo "usage: $0 [--no-build|--unsigned]" >&2
    exit 2
fi

if [ "$UNSIGNED" -eq 0 ] && [ -z "$SIGN_IDENTITY" ]; then
    # A single Developer ID certificate is the normal case. If more than one is
    # installed, require an explicit identity so a release cannot be signed by
    # the wrong account.
    # macOS ships Bash 3.2, which predates `mapfile`; keep this compatible with
    # the system shell used by a double-clicked build script.
    SIGNING_IDENTITIES=()
    while IFS= read -r identity; do
        [ -n "$identity" ] && SIGNING_IDENTITIES+=("$identity")
    done < <(security find-identity -v -p codesigning \
        | sed -n 's/.*"\(Developer ID Application: .*\)"/\1/p')
    if [ "${#SIGNING_IDENTITIES[@]}" -eq 1 ]; then
        SIGN_IDENTITY="${SIGNING_IDENTITIES[0]}"
    elif [ "${#SIGNING_IDENTITIES[@]}" -eq 0 ]; then
        echo "error: no valid Developer ID Application certificate is installed." >&2
        echo "Install the certificate for the release account, then rerun this command." >&2
        exit 1
    else
        echo "error: multiple Developer ID certificates are installed." >&2
        echo "Set DEVELOPER_ID_APPLICATION to the identity for this release." >&2
        exit 1
    fi
fi

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
    "$CALENDAR_PYINSTALLER" --noconfirm LoCal.spec
fi

[ -d "$APP" ] || { echo "error: $APP not found (run without --no-build)" >&2; exit 1; }

# Clear build-machine metadata before signing. Removing attributes after the
# signature is created can invalidate an otherwise correct app bundle.
xattr -cr "$APP" 2>/dev/null || true

if [ "$UNSIGNED" -eq 1 ]; then
    echo "==> Ad-hoc signing ${APP_NAME}.app (development only)"
    codesign --force --deep --sign - "$APP"
else
    echo "==> Signing ${APP_NAME}.app as ${SIGN_IDENTITY}"
    codesign --force --deep --options runtime --timestamp --sign "$SIGN_IDENTITY" "$APP"
    codesign --verify --deep --strict --verbose=2 "$APP"
fi

echo "==> Staging"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
# The drag target. A symlink, so it costs no space in the image and always
# points at the Applications folder of whoever mounts it.
ln -s /Applications "$STAGE/Applications"

echo "==> Creating $DMG"
hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$STAGE" \
    -ov \
    -format UDZO \
    -quiet \
    "$DMG"

if [ "$UNSIGNED" -eq 0 ]; then
    echo "==> Signing ${APP_NAME}.dmg"
    codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG"
    codesign --verify --verbose=2 "$DMG"
    # Final verification occurs after every packaging operation, not merely
    # immediately after signing the app.
    codesign --verify --deep --strict --verbose=2 "$APP"
fi

rm -rf "$STAGE"
echo "==> $DMG ($(du -h "$DMG" | cut -f1))"
