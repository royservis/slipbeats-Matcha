#!/bin/bash
# Double-click this file in Finder to start Slipbeats.
cd "$(dirname "$0")"
if ! python3 -c "import mutagen, rapidfuzz" 2>/dev/null; then
  echo "Installing Python dependencies (first run only)…"
  python3 -m pip install --user -q mutagen rapidfuzz || python3 -m pip install --user -q --break-system-packages mutagen rapidfuzz
fi
python3 -m slipbeats serve "$@"
