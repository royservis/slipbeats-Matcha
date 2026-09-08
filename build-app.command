#!/bin/bash
# Builds Slipbeats.app on this Mac. Double-click in Finder, or run from Terminal.
# Result: dist/Slipbeats.app  (drag it to /Applications)
set -e
cd "$(dirname "$0")"

echo "== Slipbeats app builder =="

# 0. Apple Command Line Tools (provides lipo/codesign used at the end of the build)
if ! xcode-select -p >/dev/null 2>&1; then
  echo
  echo "Apple's Command Line Tools are needed once. Opening the installer…"
  xcode-select --install 2>/dev/null || true
  echo "Click Install in the dialog, wait for it to finish, then run this script again."
  echo
  read -n 1 -s -r -p "Press any key to close."
  exit 1
fi

# 1. Find a Python 3.10+ (macOS's built-in one is 3.9, which is too old)
PY=""
for c in python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    v=$("$c" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)
    if [ "$v" -ge 310 ]; then PY="$c"; break; fi
  fi
done
if [ -z "$PY" ]; then
  echo
  echo "Python 3.10 or newer is needed to build the app and none was found."
  echo "Easiest fix: install Python from https://www.python.org/downloads/macos/ (the 'macOS 64-bit universal2 installer'),"
  echo "then double-click this file again."
  echo
  read -n 1 -s -r -p "Press any key to close."
  exit 1
fi
echo "Using $($PY --version) at $(command -v $PY)"

# 2. Isolated build environment
if [ ! -d .venv ]; then "$PY" -m venv .venv; fi
source .venv/bin/activate
python -m pip install --upgrade pip -q
python -m pip install -q mutagen rapidfuzz pywebview pyinstaller pillow certifi

# 3. App icon (.icns from assets/icon.png)
if [ ! -f assets/Slipbeats.icns ]; then
  echo "Making icon…"
  rm -rf assets/Slipbeats.iconset && mkdir -p assets/Slipbeats.iconset
  for s in 16 32 128 256 512; do
    sips -z $s $s assets/icon.png --out assets/Slipbeats.iconset/icon_${s}x${s}.png >/dev/null
    d=$((s*2)); sips -z $d $d assets/icon.png --out assets/Slipbeats.iconset/icon_${s}x${s}@2x.png >/dev/null
  done
  iconutil -c icns assets/Slipbeats.iconset -o assets/Slipbeats.icns
  rm -rf assets/Slipbeats.iconset
fi

# 4. Build
rm -rf build dist
python -m PyInstaller --noconfirm --clean Slipbeats.spec

# 5. Ad-hoc sign so macOS lets it run locally (no developer account needed)
codesign --force --deep --sign - dist/Slipbeats.app >/dev/null 2>&1 || true
xattr -dr com.apple.quarantine dist/Slipbeats.app 2>/dev/null || true

echo
echo "Built: $(pwd)/dist/Slipbeats.app"
echo "Drag it to /Applications. First launch: right-click → Open if Gatekeeper complains."
open dist
