#!/usr/bin/env bash
# Capture one README screenshot with headless Chrome.
#
#   capture.sh <name> <url-path-with-query-and-hash> [window-size]
#
# Expects a harnessed tome (see inject.py) already serving on $PORT. On WSL the
# Chrome binary is a Windows .exe, so --screenshot needs a Windows path while we
# read the result back through /mnt/c — hence the two forms of $OUT below.
set -euo pipefail

CHROME="${CHROME:-/mnt/c/Program Files/Google/Chrome/Application/chrome.exe}"
OUT_WIN="${OUT_WIN:-C:\\Users\\$USER\\AppData\\Local\\Temp\\tome-shots}"
OUT_WSL="${OUT_WSL:-/mnt/c/Users/$USER/AppData/Local/Temp/tome-shots}"
PORT="${PORT:-7970}"

name="$1"
path="$2"
size="${3:-1600,1000}"

mkdir -p "$OUT_WSL"
"$CHROME" --headless=new --disable-gpu --hide-scrollbars \
  --window-size="$size" --virtual-time-budget=6000 \
  --screenshot="$OUT_WIN\\$name.png" \
  "http://127.0.0.1:$PORT$path" >/dev/null 2>&1

printf '%s  (%s bytes)\n' "$OUT_WSL/$name.png" "$(stat -c%s "$OUT_WSL/$name.png")"
