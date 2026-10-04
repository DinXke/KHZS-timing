"""Passieve bewaking van de SwimTime-pc (enkel luisteren, niets verzenden).

Schrijft naar logs/bewaking.log en stdout:
  - broadcast stil (gat > GAP s), met duur
  - herstart SwimTime (bronpoort van de broadcast verandert)
  - SMB-verkeer (tcp/445) van/naar de SwimTime-pc, gegroepeerd per burst
  - start/stop van de live-server (livetiming.py)
"""
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

TSHARK = r"C:\Program Files\Wireshark\tshark.exe"
HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.0.188"
IFACE = sys.argv[2] if len(sys.argv) > 2 else "Wi-Fi"
GAP = 1.5
SMB_IDLE = 3.0
LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "bewaking.log")
lock = threading.Lock()


def ts(t=None):
    return datetime.fromtimestamp(t or time.time()).strftime("%H:%M:%S")


def emit(msg, quiet=False):
    """quiet=True: enkel in het logbestand (bv. SMB-keepalives), niet op stdout."""
    line = f"{ts()} {msg}"
    with lock:
        if not quiet:
            print(line, flush=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def server_running():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "@(Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                          "Where-Object { $_.CommandLine -like '*livetiming.py*' -and $_.CommandLine -notlike '*replay*' }).Count"],
                         capture_output=True, text=True).stdout.strip()
    return out not in ("", "0")


def watch_server():
    prev = None
    while True:
        try:
            cur = server_running()
            if cur != prev:
                emit("LIVE-SERVER " + ("draait" if cur else "GESTOPT"))
                prev = cur
        except Exception:
            pass
        time.sleep(5)


state = {"last": None, "port": None, "silent": False, "smb_start": None, "smb_last": None, "smb_in": 0, "smb_out": 0}


def watch_timers():
    while True:
        time.sleep(0.5)
        now = time.time()
        with lock:
            s = dict(state)
        if s["last"] and not s["silent"] and now - s["last"] > GAP:
            state["silent"] = True
            emit(f"BROADCAST STIL sinds {ts(s['last'])}")
        if s["smb_start"] and now - s["smb_last"] > SMB_IDLE:
            emit(f"SMB {ts(s['smb_start'])}-{ts(s['smb_last'])}: laptop->188 {s['smb_out'] // 1024} kB, "
                 f"188->laptop {s['smb_in'] // 1024} kB", quiet=(s["smb_in"] + s["smb_out"]) < 2048)
            state.update(smb_start=None, smb_in=0, smb_out=0)


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    emit(f"bewaking gestart (host {HOST}, interface {IFACE}, passief)")
    threading.Thread(target=watch_server, daemon=True).start()
    threading.Thread(target=watch_timers, daemon=True).start()
    cmd = [TSHARK, "-l", "-n", "-Q", "-p", "-i", IFACE, "-f", f"host {HOST}",
           "-T", "fields", "-e", "frame.time_epoch", "-e", "ip.src", "-e", "udp.dstport", "-e", "udp.srcport",
           "-e", "tcp.port", "-e", "frame.len"]
    while True:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in p.stdout:
            f = line.rstrip("\n").split("\t")
            if len(f) < 6:
                continue
            t, src, udst, usrc, tports, ln = float(f[0]), f[1], f[2], f[3], f[4], int(f[5] or 0)
            if udst == "26" and src == HOST:
                if state["silent"]:
                    emit(f"BROADCAST TERUG na {t - state['last']:.1f} s stilte")
                    state["silent"] = False
                if state["port"] and usrc != state["port"]:
                    emit(f"HERSTART SWIMTIME: bronpoort {state['port']} -> {usrc}")
                state["port"] = usrc
                state["last"] = t
            elif "445" in tports.split(","):
                if state["smb_start"] is None:
                    state["smb_start"] = t
                    emit("SMB-verkeer gestart " + ("(laptop -> 188)" if src != HOST else "(188 -> laptop)"), quiet=True)
                state["smb_last"] = t
                if src == HOST:
                    state["smb_in"] += ln
                else:
                    state["smb_out"] += ln
        emit("tshark gestopt, herstart over 3 s")
        time.sleep(3)


if __name__ == "__main__":
    main()
