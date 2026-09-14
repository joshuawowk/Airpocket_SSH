#!/usr/bin/env bash
# Linux/macOS equivalent of tools/flash_tab5.ps1 — full device flash over USB.
#
#   tools/flash_tab5.sh [--port /dev/ttyACM0] [--erase] [--local-profiles]
#
# --local-profiles temporarily swaps data/profiles.local.json (gitignored) in as
# data/profiles.json so the LittleFS image carries your real credentials, then
# restores the public file.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="/dev/ttyACM0"
BAUD=1500000
ERASE=0
LOCAL_PROFILES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --baud) BAUD="$2"; shift 2 ;;
        --erase) ERASE=1; shift ;;
        --local-profiles) LOCAL_PROFILES=1; shift ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

PIO="${PIO:-$(command -v pio || echo "$HOME/.platformio/penv/bin/pio")}"
ESPTOOL="${ESPTOOL:-$HOME/.platformio/penv/bin/esptool}"
BUILD="$ROOT/.pio/build/tab5"

restore_profiles() {
    if [ -n "${PROFILE_BACKUP:-}" ] && [ -f "$PROFILE_BACKUP" ]; then
        mv -f "$PROFILE_BACKUP" "$ROOT/data/profiles.json"
        echo "Restored public data/profiles.json"
    fi
}
trap restore_profiles EXIT

if [ "$LOCAL_PROFILES" = 1 ] && [ -f "$ROOT/data/profiles.local.json" ]; then
    PROFILE_BACKUP="$(mktemp)"
    cp -f "$ROOT/data/profiles.json" "$PROFILE_BACKUP"
    cp -f "$ROOT/data/profiles.local.json" "$ROOT/data/profiles.json"
    echo "Using ignored local profiles: data/profiles.local.json"
fi

export PYTHONUTF8=1
"$PIO" run -e tab5
"$PIO" run -e tab5 -t buildfs

if [ "$ERASE" = 1 ]; then
    "$ESPTOOL" --chip esp32p4 --port "$PORT" --baud "$BAUD" erase-flash
fi

"$ESPTOOL" --chip esp32p4 --port "$PORT" --baud "$BAUD" write-flash \
    0x2000   "$BUILD/bootloader.bin" \
    0x8000   "$BUILD/partitions.bin" \
    0x10000  "$BUILD/firmware.bin" \
    0x410000 "$BUILD/littlefs.bin"

echo "Flashed Tab5 on $PORT"
