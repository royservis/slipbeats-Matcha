# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Slipbeats.app (macOS). Build with build-app.command.
from PyInstaller.utils.hooks import collect_submodules
import re
VERSION = re.search(r'VERSION = "([^"]+)"', open("slipbeats/__init__.py").read()).group(1)

a = Analysis(
    ["slipbeats_app.py"],
    pathex=["."],
    binaries=[],
    datas=[("static", "static")],
    hiddenimports=collect_submodules("mutagen") + ["certifi", "rapidfuzz", "rapidfuzz.fuzz", "rapidfuzz.process", "webview.platforms.cocoa"],
    hookspath=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Slipbeats",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=None,          # native arch of the machine that builds it (arm64 on the M1)
    icon="assets/Slipbeats.icns",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Slipbeats")
app = BUNDLE(
    coll,
    name="Slipbeats.app",
    icon="assets/Slipbeats.icns",
    bundle_identifier="io.slipbeats.app",
    info_plist={
        "CFBundleName": "Slipbeats",
        "CFBundleDisplayName": "Slipbeats",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.3",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "LSApplicationCategoryType": "public.app-category.music",
    },
)
