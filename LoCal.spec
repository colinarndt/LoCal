# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the menu bar app.

    pyinstaller --noconfirm LoCal.spec    # -> dist/LoCal.app

PyInstaller rather than py2app: py2app's newest release (0.28.10) reads
`[project].dependencies` out of pyproject.toml as `install_requires` and then
rejects it, which current setuptools has no way around.

The bundle is code only. Everything the app writes -- database, flyers, avatars,
settings, keys -- resolves through `social_calendar.paths` into Application
Support. If the built app approaches 250MB, that separation has broken and the
200MB media directory has been swept inside it, where it would break signing and
be destroyed on every update.
"""

a = Analysis(
    # Not social_calendar/app.py directly: PyInstaller runs its entry script as
    # `__main__` with no parent package, which breaks that module's relative
    # imports. See launcher.py.
    ["launcher.py"],
    pathex=["."],
    binaries=[],
    # Jinja loads these off the filesystem rather than by import, so nothing in
    # the dependency graph points at them. Undeclared, every page 500s.
    datas=[
        ("social_calendar/templates", "social_calendar/templates"),
        ("social_calendar/static", "social_calendar/static"),
    ],
    hiddenimports=[
        # Reached only through `from apify_client import ApifyClient` inside a
        # function body, which the static analysis does not follow.
        "apify_client",
        # Flask's dev server; imported lazily by app.run().
        "werkzeug.serving",
        # openai resolves its transport lazily, so nothing in the graph points
        # at these and the frozen app 500s on the first model call without them.
        "openai",
        "httpx",
        # Selected at runtime by name, so no import edge exists to follow.
        "social_calendar.web",
        "social_calendar.cli",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "tkinter", "numpy", "PIL", "matplotlib", "pyinstaller"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LoCal",
    debug=False,
    strip=False,
    upx=False,          # UPX-compressed binaries fail Gatekeeper on arm64
    console=False,      # no terminal window
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LoCal",
)

app = BUNDLE(
    coll,
    name="LoCal.app",
    # Drawn by make_icon.py, which is the editable source for it.
    icon="AppIcon.icns",
    bundle_identifier="com.colinarndt.local-calendar",
    info_plist={
        "CFBundleName": "LoCal",
        "CFBundleDisplayName": "LoCal",
        "CFBundleShortVersionString": "0.3.0",
        "CFBundleVersion": "0.3.0",
        # Menu bar only: no Dock tile, no app menu bar takeover.
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        # Flask and the WKWebView talk plain HTTP over 127.0.0.1. Without this,
        # App Transport Security blocks the webview and the window renders blank.
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
