#!/usr/bin/env python3
"""Headless installer for Airpocket_SSH onto an M5Stack Tab5 running the
M5Stack Launcher (bmorcelli fork) — no touchscreen, no USB-MSC.

The Launcher exposes a line-based console on the Tab5's USB-Serial-JTAG port.
This script drives it.

    # what is on the device right now
    tools/launcher_install.py --partitions

    # RECOMMENDED: install the app straight into a new OTA slot, creating the
    # 1 MB LittleFS data partition first. Never touches the microSD.
    tools/launcher_install.py app --create-littlefs

    # alternative: stage the merged factory image on the SD and let the Launcher
    # install it (this is the path that also copies profiles.json into LittleFS)
    tools/launcher_install.py sd

    # go back to the Launcher (clears otadata so the bootloader picks factory)
    tools/launcher_install.py --boot-launcher

Why --create-littlefs exists: the Launcher sizes a guest app's data partition
from the size declared in the image's own partition table. A partition table
that hands LittleFS "the rest of the chip" cannot be satisfied on a Tab5 that
already hosts the Launcher plus another app, and the install stalls on the
Launcher's interactive "not enough space" chooser. See docs/LAUNCHER.md.
"""

import argparse
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.expanduser("~/.platformio/penv/lib/python3.10/site-packages"))
try:
    import serial  # type: ignore
except ImportError:  # pragma: no cover
    sys.exit("pyserial not found; try: ~/.platformio/penv/bin/python -m pip install pyserial")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, ".pio", "build", "tab5")
FACTORY = os.path.join(ROOT, "dist", "Airpocket_SSH-tab5-factory.bin")
ESPTOOL = os.environ.get("ESPTOOL", os.path.expanduser("~/.platformio/penv/bin/esptool"))

CHUNK = 2048
ACK_RE = re.compile(rb"ACK (\d+)/")


class Console:
    """The Launcher's serial console. Opened without toggling DTR/RTS, which
    would reset the P4 out from under us."""

    def __init__(self, port, baud=115200):
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = port, baud, 0
        s.dtr = s.rts = False
        s.open()
        self.s = s
        self.buf = bytearray()
        time.sleep(0.3)
        self.drain(1.0)

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

    def _read(self):
        n = self.s.in_waiting
        if n:
            self.buf.extend(self.s.read(n))
        else:
            time.sleep(0.002)

    def drain(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            n = self.s.in_waiting
            if n:
                self.s.read(n)
                end = time.time() + 0.25
            else:
                time.sleep(0.02)
        try:
            self.s.reset_input_buffer()
        except Exception:
            pass
        self.buf.clear()

    def send(self, line):
        self.s.write((line + "\r\n").encode())
        self.s.flush()

    def collect(self, seconds, idle=0.8):
        end = time.time() + seconds
        last = time.time()
        out = bytearray()
        while time.time() < end:
            n = self.s.in_waiting
            if n:
                d = self.s.read(n)
                out.extend(d)
                last = time.time()
            else:
                if out and time.time() - last > idle:
                    break
                time.sleep(0.02)
        return out.decode("utf-8", "replace")

    def command(self, line, seconds=6.0):
        self.drain(0.3)
        self.send(line)
        return self.collect(seconds)

    def wait_for(self, needles, seconds):
        """Accumulate until one of `needles` (bytes) shows up. Returns the hit or None."""
        end = time.time() + seconds
        while time.time() < end:
            self._read()
            for n in needles:
                if n in self.buf:
                    return n
        return None

    def last_ack(self):
        m = None
        for mm in ACK_RE.finditer(self.buf):
            m = mm
        return int(m.group(1)) if m else -1

    def stream(self, data, label):
        """Stream `data` using the Launcher's READY / chunked-ACK protocol."""
        size = len(data)
        self.buf.clear()
        off, t0 = 0, time.time()
        while off < size:
            chunk = data[off:off + CHUNK]
            self.s.write(chunk)
            self.s.flush()
            off += len(chunk)
            deadline = time.time() + 20
            while self.last_ack() < off:
                self._read()
                if b"ERR" in self.buf:
                    raise RuntimeError(self.buf[-240:].decode("utf-8", "replace"))
                if time.time() > deadline:
                    raise RuntimeError(f"timeout at {off}/{size} (last ACK {self.last_ack()})")
            if len(self.buf) > 2048:
                del self.buf[:-128]
            if off % (CHUNK * 400) == 0 or off == size:
                rate = off / max(0.1, time.time() - t0) / 1024
                print(f"  {label} {off}/{size} {100 * off // size}% {rate:.0f} KB/s", flush=True)


def read_blob(path):
    if not os.path.exists(path):
        sys.exit(f"missing {path} — build it first (tools/build_factory_bin.sh)")
    with open(path, "rb") as fh:
        return fh.read()


def cmd_partitions(con, _args):
    print(con.command("partitions", 6))


def cmd_app(con, args):
    """`flash firmware` — stream the app-only image into a fresh OTA slot."""
    if args.create_littlefs:
        out = con.command(f"partition create data littlefs littlefs {hex(args.littlefs_size)}", 8)
        print(out.strip())
        if "OK partition created" not in out and "ERR" in out:
            sys.exit("could not create the littlefs partition")
        elif "OK partition created" not in out:
            print("note: no 'OK partition created' seen; it may already exist", file=sys.stderr)

    data = read_blob(args.image or os.path.join(BUILD, "firmware.bin"))
    if data[:1] != b"\xe9":
        sys.exit("that is not an app image (first byte must be 0xE9) — use the `sd` mode for a factory image")
    print(f"flash firmware {args.name} {len(data)}")
    con.drain(0.5)
    con.send(f"flash firmware {args.name} {len(data)}")
    if con.wait_for([b"READY"], 25) is None:
        sys.exit("no READY: " + con.buf[-240:].decode("utf-8", "replace"))
    con.stream(data, "app")
    hit = con.wait_for([b"OK flashed", b"ERR"], 60)
    print(con.buf[-400:].decode("utf-8", "replace"))
    if hit != b"OK flashed":
        sys.exit("install failed")
    print("\nInstalled. The Tab5 is rebooting into Airpocket SSH.")


def cmd_sd(con, args):
    """`sdput` + `sdinstall` — stage the factory image on the microSD, then let
    the Launcher parse its partition table and install app + data partitions."""
    data = read_blob(args.image or FACTORY)
    path = args.sdpath
    print(f"sdput {path} ({len(data)} bytes)")
    con.drain(0.5)
    con.send(f"sdput {path} {len(data)}")
    if con.wait_for([b"READY"], 25) is None:
        sys.exit("no READY: " + con.buf[-240:].decode("utf-8", "replace"))
    con.stream(data, "sd")
    if con.wait_for([b"OK stored", b"ERR"], 60) != b"OK stored":
        sys.exit("sdput failed: " + con.buf[-240:].decode("utf-8", "replace"))
    print("stored on SD; installing")

    con.drain(0.5)
    con.send(f"sdinstall {path}")
    # sdinstall reboots on success and never prints a completion line, so watch
    # for an error or for the console going quiet after the reboot.
    end = time.time() + args.install_timeout
    con.buf.clear()
    while time.time() < end:
        con._read()
        if b"ERR" in con.buf or b"Update Error" in con.buf or b"No install space" in con.buf:
            print(con.buf[-600:].decode("utf-8", "replace"))
            sys.exit("install failed — see docs/LAUNCHER.md")
    print(con.buf[-600:].decode("utf-8", "replace"))
    print("\nsdinstall finished (or is rebooting). Check with --partitions after reset.")


def cmd_boot_launcher(args):
    """Erase otadata so the ROM bootloader falls back to the factory app (the
    Launcher). This is how you leave an installed guest app headlessly."""
    subprocess.check_call([
        ESPTOOL, "--chip", "esp32p4", "--port", args.port, "erase-region", "0xe000", "0x2000",
    ])
    print("otadata cleared — the Tab5 now boots into the Launcher.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default="/dev/ttyACM0")
    p.add_argument("--partitions", action="store_true", help="print the device partition table and exit")
    p.add_argument("--boot-launcher", action="store_true", help="clear otadata so the Tab5 boots the Launcher")
    sub = p.add_subparsers(dest="mode")

    a = sub.add_parser("app", help="stream the app image into a new OTA slot (does not touch the SD)")
    a.add_argument("--name", default="AirpocketSSH")
    a.add_argument("--image", help="default: .pio/build/tab5/firmware.bin")
    a.add_argument("--create-littlefs", action="store_true", help="create the 'littlefs' data partition first")
    a.add_argument("--littlefs-size", type=lambda v: int(v, 0), default=0x100000)

    s = sub.add_parser("sd", help="sdput the factory image, then sdinstall it")
    s.add_argument("--image", help="default: dist/Airpocket_SSH-tab5-factory.bin")
    s.add_argument("--sdpath", default="/airpocket_ssh.bin")
    s.add_argument("--install-timeout", type=float, default=180.0)

    args = p.parse_args()

    if args.boot_launcher:
        return cmd_boot_launcher(args)
    if not args.partitions and not args.mode:
        p.print_help()
        return

    con = Console(args.port)
    try:
        if args.partitions:
            cmd_partitions(con, args)
        elif args.mode == "app":
            cmd_app(con, args)
        elif args.mode == "sd":
            cmd_sd(con, args)
    finally:
        con.close()


if __name__ == "__main__":
    main()
