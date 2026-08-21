# Installing through the M5Stack Launcher

Airpocket SSH can be flashed straight to the Tab5 with esptool
(`tools/flash_tab5.sh`), or installed as a *guest app* alongside other firmware
using the [bmorcelli M5Stack Launcher](https://github.com/bmorcelli/Launcher).
This document covers the Launcher path, because it has one non-obvious
requirement that will otherwise make the install fail at the very end.

## How the Launcher installs a guest app

The Launcher is a chain-loader that lives in the `factory` partition. To install
a guest app it:

1. reads the app's **full-flash factory image** (bootloader `0x2000`, partition
   table `0x8000`, app `0x10000`, data partitions after that);
2. parses the partition table embedded in that image at `0x8000`;
3. carves a new OTA app partition, plus one partition per declared data
   partition, out of whatever flash it is not already using;
4. copies the app and the data images in, rewrites the on-device partition
   table, points `otadata` at the new app, and reboots.

Step 3 is the catch. On a 16 MB Tab5 the Launcher itself, its `nvs`/`otadata`/
`coredump` partitions and any already-installed app are *not* available — a
typical device has around **9–10 MB of free flash**, not 16 MB.

## The partition-size requirement

`partitions.csv` is not just the direct-flash layout; it is also the request the
Launcher has to satisfy. The Launcher sizes a guest data partition from the size
**declared in that table** (`sd_functions.cpp`, `updateFromSD`): for a partition
whose label is not literally `spiffs` and whose declared size is larger than the
Launcher's default (`0x70000` on a 16 MB device), it allocates the full declared
size — it will not silently shrink it.

So a "fill the rest of the chip" LittleFS partition is fatal here. The original
layout declared:

```text
app0     0x400000   (4 MB)
littlefs 0xBF0000   (11.9 MB)      <-- ~15.9 MB required, ~9.75 MB available
```

The Launcher cannot lay that out, falls through to its interactive
"not enough space" chooser (`launcherSelectInstallLayout` →
`while (selected == 0) selected = loopOptions(choices);`) and blocks there
waiting for a touch that a headless install will never deliver — the symptom is
the Tab5 appearing to lock up or reset near the end of the install.

The layout now in `partitions.csv` keeps the whole firmware inside ~5 MB:

```text
app0     0x400000   (4 MB)   - firmware.bin is ~3.2 MB
littlefs 0x100000   (1 MB)   - only holds profiles.json
```

That is well inside the free space on a Tab5 that already hosts the Launcher and
one other app. **If you grow `littlefs` again, keep the total under the device's
free flash** — check it with `tools/launcher_install.py --partitions`.

## Installing

Build the merged factory image first:

```bash
tools/build_factory_bin.sh          # -> dist/Airpocket_SSH-tab5-factory.bin
```

### Option A — app-only, no microSD (recommended)

Streams the app image straight into a new OTA slot over the Launcher's serial
console and creates the LittleFS data partition explicitly. Nothing is written
to the microSD, so this works even if the card is unhealthy.

```bash
tools/launcher_install.py app --create-littlefs
```

LittleFS starts out empty; the firmware formats it on first mount and you
configure Wi-Fi/SSH profiles on the device. To preload `data/profiles.json`
instead, write the built image into the partition directly (the offset comes
from `--partitions`):

```bash
~/.platformio/penv/bin/esptool --chip esp32p4 --port /dev/ttyACM0 \
    write-flash 0x640000 .pio/build/tab5/littlefs.bin
```

### Option B — through the microSD

Stages the factory image on the card with `sdput`, then has the Launcher install
it with `sdinstall`. This path also copies `profiles.json` into LittleFS for you.

```bash
tools/launcher_install.py sd
```

You can also copy `dist/Airpocket_SSH-tab5-factory.bin` to the SD root by hand
and pick it from the Launcher's **SD** tile.

> **Do not use the Launcher's USB-MSC tile to copy the file.** Exposing the card
> as a host block device and then ejecting it uncleanly leaves the FAT and the
> root directory inconsistent — the card still mounts and still reports used
> space, but the files are gone. `sdput` goes through FATFS and is safe.

## Switching back to the Launcher

Installing sets `otadata` to the new app, so the ROM bootloader goes straight
into Airpocket SSH and the Launcher never runs. Clearing `otadata` makes the
bootloader fall back to the `factory` partition:

```bash
tools/launcher_install.py --boot-launcher
```

From the Launcher you can pick either installed app from the boot menu, or set
**CFG → `[x] Boot to Launcher`** so it stops auto-booting a guest app.

## microSD troubleshooting

The firmware reports the card state on every boot (`[boot] SD ready: ...` or
`[boot] SD card not detected`) and through `sd status` on the serial API.
`ensureSdReady()` retries the mount six times with a settle delay and then
re-tries on later commands, so a one-off miss after a warm reset no longer
disables the card for the rest of the session.

Distinguish three different failures — they look similar on the device but need
very different responses:

| Symptom | Meaning | Fix |
|---|---|---|
| `Failed to mount SDCARD` right after leaving the Launcher's USB tile | The card is *latched*, not damaged. A warm reset does not clear it because the Tab5's battery keeps the card powered. | Power-cycle the Tab5, or reseat the card. |
| Card mounts, `sd df` shows space used, but `sd ls /` is empty and known files are missing | The FAT and/or root directory are damaged. Read the card in a PC card reader and run `fsck.vfat -v`. | Recover what you need, then reformat FAT32. |
| `f_mount failed: (13) There is no valid FAT volume`, or a freshly written filesystem reads back as garbage | The card is failing — it stops retaining what was written to it. | Replace the card. |

Any replacement must be **FAT32**. Cards up to 32 GB ship that way; larger cards
ship exFAT, which the Arduino SD library used here cannot read, so reformat them
(`mkfs.vfat -F 32 -n TAB5 /dev/sdX1`, or the SD Association formatter).

Format a replacement in a PC card reader, not through the Launcher's USB tile —
that path runs at roughly 100 KB/s, so writing the ~8 MB of FAT tables alone
takes minutes, and it is the same path that leaves the card latched on exit.

## Recovery

Everything here is recoverable over USB — the Tab5's ESP32-P4 exposes
USB-Serial-JTAG at `/dev/ttyACM0` (`303a:1001`) and esptool can always reach it.

```bash
# back to a bare Launcher (rewrites bootloader + partition table + factory app)
~/.platformio/penv/bin/esptool --chip esp32p4 --port /dev/ttyACM0 \
    write-flash 0x0 Launcher-m5stack-tab5.bin

# or drop the Launcher entirely and run Airpocket SSH as the factory app
tools/flash_tab5.sh --port /dev/ttyACM0 --erase
```

Large `read-flash` operations over USB-Serial-JTAG are unreliable ("Packet
content transfer stopped"); read in chunks of 256 KB or less if you need a
backup. Writes are reliable.
