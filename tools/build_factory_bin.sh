#!/usr/bin/env bash
# Build the Tab5 firmware and merge it into a single full-flash "factory" image.
#
# The merged image is what you want for:
#   * a one-shot esptool flash at offset 0x0
#   * installing through the M5Stack Launcher (bmorcelli fork) SD menu / `sdinstall`,
#     which reads the partition table out of the image at 0x8000 and repartitions
#     the Tab5's flash to make room for the app + its LittleFS data partition
#
# Output: dist/Airpocket_SSH-tab5-factory.bin
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PIO="${PIO:-$(command -v pio || echo "$HOME/.platformio/penv/bin/pio")}"
ESPTOOL="${ESPTOOL:-$HOME/.platformio/penv/bin/esptool}"
BUILD="$ROOT/.pio/build/tab5"
OUT="$ROOT/dist/Airpocket_SSH-tab5-factory.bin"

[ -x "$PIO" ] || { echo "pio not found (set PIO=...)" >&2; exit 1; }
[ -x "$ESPTOOL" ] || { echo "esptool not found (set ESPTOOL=...)" >&2; exit 1; }

export PYTHONUTF8=1
"$PIO" run -e tab5
"$PIO" run -e tab5 -t buildfs

mkdir -p "$ROOT/dist"
"$ESPTOOL" --chip esp32p4 merge-bin -o "$OUT" \
    --flash-mode qio --flash-freq 80m --flash-size 16MB \
    0x2000   "$BUILD/bootloader.bin" \
    0x8000   "$BUILD/partitions.bin" \
    0x10000  "$BUILD/firmware.bin" \
    0x410000 "$BUILD/littlefs.bin"

echo
echo "Factory image: $OUT"
ls -l "$OUT"
