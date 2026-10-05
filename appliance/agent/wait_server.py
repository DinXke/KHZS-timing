#!/usr/bin/env python3
"""Wacht (max. 60 s) tot de live-timingserver antwoordt, zodat het HDMI-scherm niet met een foutpagina start."""
import time
import urllib.request

for _ in range(60):
    try:
        urllib.request.urlopen("http://127.0.0.1/api/version", timeout=1)
        break
    except Exception:
        time.sleep(1)
