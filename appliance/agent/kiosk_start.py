#!/usr/bin/env python3
"""Start van het HDMI-scherm binnen cage: eerst resolutie en draaiing instellen (wlr-randr), dan Chromium.

KIOSK_MODE   = auto | 1920x1080 | 1280x720 | …   (auto = wat de tv als voorkeur opgeeft, via EDID)
KIOSK_ROTATE = 0 | 90 | 180 | 270                (staand scherm, bv. in de oproepkamer)
Een fout bij het instellen houdt het scherm nooit tegen: Chromium start altijd.
"""
import os
import re
import shutil
import subprocess
import sys


def outputs():
    try:
        out = subprocess.run(["wlr-randr"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.split()[0] for line in out.splitlines() if line and not line[0].isspace()]


def main():
    mode = os.environ.get("KIOSK_MODE", "auto").strip()
    rot = os.environ.get("KIOSK_ROTATE", "0").strip()
    args = []
    if mode and mode != "auto" and re.fullmatch(r"\d{3,4}x\d{3,4}(@\d+(\.\d+)?Hz)?", mode):
        args += ["--mode", mode]
    if rot in ("90", "180", "270"):
        args += ["--transform", rot]
    if args and shutil.which("wlr-randr"):
        for o in outputs():
            r = subprocess.run(["wlr-randr", "--output", o] + args, capture_output=True, text=True, timeout=10)
            print(f"scherm {o}: {' '.join(args)} -> {'ok' if r.returncode == 0 else r.stderr.strip()}", flush=True)
    os.execv("/usr/bin/chromium", ["chromium"] + sys.argv[1:])


if __name__ == "__main__":
    main()
