#!/usr/bin/env python3
"""HZS Timing – beheerdienst op de cloudserver (root, enkel 127.0.0.1:8092).

De publieke relay (livetiming.py --relay-server, gebruiker 'livetiming') stuurt beheerverzoeken van de lokale server
hierheen nadat het relay-token gecontroleerd is (route /admin/* via https://timing.khzs.be). Deze dienst vraagt
daarnaast een eigen sleutel (X-Agent-Key, /etc/livetiming-agent.key) en weigert alles met een Origin-header.

  GET  /status                         systeem, diensten, versies
  GET  /logs?unit=livetiming&n=200     logboek (livetiming | cloudflared | agent)
  POST /restart {"unit": ...}          livetiming | cloudflared | agent
  POST /update  (zip)                  release van de lokale server installeren (zelftest, terugval)
  POST /console/open {"password"}      root-console (PTY); wachtwoord apart, enkel als hash bewaard
  GET  /console/<id>/read?pos=N        uitvoer vanaf positie N (wacht max. 20 s op nieuwe uitvoer)
  POST /console/<id>/write {"data"}    invoer (base64)
  POST /console/<id>/resize {"cols","rows"}
  POST /console/<id>/close
  POST /console/password {"old","new"} consolewachtwoord wijzigen

Consolewachtwoord instellen op de server zelf:  python3 /opt/livetiming-agent/cloud_agent.py --set-password
"""
import base64
import fcntl
import getpass
import hashlib
import hmac
import io
import json
import os
import pty
import secrets
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = ("127.0.0.1", 8092)
KEY_FILE = "/etc/livetiming-agent.key"
CONF_FILE = "/etc/livetiming-agent.json"
APP = "/opt/livetiming"
AGENT_DIR = "/opt/livetiming-agent"
BACKUPS = "/opt/livetiming-backups"
UNITS = {"livetiming": "livetiming", "cloudflared": "cloudflared", "agent": "livetiming-agent"}
STARTED = time.time()
LOCK = threading.Lock()
FAILS = {"n": 0, "until": 0.0}
SESSIONS = {}
MAX_SESSIONS = 3
IDLE_S = 15 * 60
MAX_S = 4 * 3600


def log(msg):
    print(msg, flush=True)


def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def load_conf():
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_conf(c):
    tmp = CONF_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONF_FILE)


def hash_pw(pw, salt=None, n=300_000):
    salt = salt or secrets.token_hex(16)
    return {"salt": salt, "n": n, "hash": hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), n).hex()}


def check_pw(pw):
    h = load_conf().get("console")
    if not h or not pw:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(h["salt"]), int(h["n"])).hex()
    return hmac.compare_digest(calc, h["hash"])


def ensure_key():
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, "w") as f:
            f.write(secrets.token_hex(32))
    try:
        import grp
        os.chown(KEY_FILE, 0, grp.getgrnam("livetiming").gr_gid)
    except (KeyError, OSError):
        pass
    os.chmod(KEY_FILE, 0o640)
    return open(KEY_FILE).read().strip()


# ---------------------------------------------------------------- status en logboek
def app_version(path=APP):
    try:
        for line in open(os.path.join(path, "livetiming.py"), encoding="utf-8"):
            if line.startswith('VERSION = "'):
                return line.split('"')[1]
    except OSError:
        pass
    return None


def status():
    def unit_state(u):
        return run(["systemctl", "is-active", u], 5)[1].strip() or "?"
    try:
        load = os.getloadavg()
    except OSError:
        load = (0, 0, 0)
    mem = {}
    try:
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    du = shutil.disk_usage("/")
    up = 0.0
    try:
        up = float(open("/proc/uptime").read().split()[0])
    except OSError:
        pass
    cf = run(["cloudflared", "--version"], 5)[1].strip().split("\n")[0]
    backups = sorted(os.listdir(BACKUPS), reverse=True)[:5] if os.path.isdir(BACKUPS) else []
    return {"hostname": os.uname().nodename, "uptime": up, "load": load, "agentUptime": time.time() - STARTED,
            "mem": {"total": mem.get("MemTotal"), "available": mem.get("MemAvailable")},
            "disk": {"total": du.total, "free": du.free}, "version": app_version(), "python": sys.version.split()[0],
            "cloudflared": cf, "units": {k: unit_state(u) for k, u in UNITS.items()},
            "console": {"configured": bool(load_conf().get("console")), "sessions": len(SESSIONS),
                        "locked": FAILS["until"] > time.time()},
            "backups": backups}


def logs(unit, n):
    u = UNITS.get(unit)
    if not u:
        raise ValueError("onbekende dienst")
    n = max(10, min(2000, int(n)))
    return run(["journalctl", "-u", u, "-n", str(n), "--no-pager", "-o", "short-iso"], 15)[1]


def restart(unit):
    u = UNITS.get(unit)
    if not u:
        raise ValueError("onbekende dienst")
    # via systemd-run zodat het antwoord nog vertrekt (ook als de agent zichzelf herstart)
    run(["systemd-run", "--on-active=1", "--quiet", "systemctl", "restart", u], 10)
    return True


# ---------------------------------------------------------------- updates
def install_release(blob):
    """Release-zip (tools/build_release.py) installeren: controle, zelftest, kopie, herstart, gezondheidscheck, terugval."""
    out = []

    def say(m):
        out.append(m)
        log("update: " + m)

    z = zipfile.ZipFile(io.BytesIO(blob))
    meta = json.loads(z.read("release.json"))
    ver = meta["version"]
    for f, h in meta["files"].items():
        if hashlib.sha256(z.read("app/" + f)).hexdigest() != h:
            raise ValueError(f"controlesom klopt niet: {f}")
    say(f"pakket {ver}: {len(meta['files'])} bestanden, controlesommen OK")
    tmp = tempfile.mkdtemp(prefix="lt-update-")
    try:
        for n in z.namelist():
            if n.startswith("app/") and not n.endswith("/"):
                p = os.path.join(tmp, n[4:])
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(z.read(n))
        # zelftest met een kopie van de instellingen
        st = os.path.join(tmp, "selftest-settings.json")
        if os.path.exists(os.path.join(APP, "settings.json")):
            shutil.copy(os.path.join(APP, "settings.json"), st)
        rc, so, se = run([sys.executable, os.path.join(tmp, "livetiming.py"), "--selftest", "--settings", st,
                          "--history", os.path.join(tmp, "h.json")], 60)
        if rc != 0:
            raise RuntimeError("zelftest mislukt: " + (se or so).strip()[-400:])
        say("zelftest OK")
        # reservekopie van wat er nu draait
        bdir = os.path.join(BACKUPS, time.strftime("%Y%m%d-%H%M%S") + "-" + (app_version() or "onbekend"))
        os.makedirs(bdir, exist_ok=True)
        os.makedirs(APP, exist_ok=True)
        for item in ("livetiming.py", "static"):
            src = os.path.join(APP, item)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(bdir, item))
            elif os.path.exists(src):
                shutil.copy2(src, bdir)
        if os.path.exists(os.path.join(AGENT_DIR, "cloud_agent.py")):
            shutil.copy2(os.path.join(AGENT_DIR, "cloud_agent.py"), bdir)
        say("reservekopie: " + bdir)
        # nieuwe code plaatsen (instellingen en cache blijven)
        shutil.copy2(os.path.join(tmp, "livetiming.py"), os.path.join(APP, "livetiming.py"))
        if os.path.isdir(os.path.join(tmp, "static")):
            shutil.copytree(os.path.join(tmp, "static"), os.path.join(APP, "static"), dirs_exist_ok=True)
        for extra in ("README.md",):
            if os.path.exists(os.path.join(tmp, extra)):
                shutil.copy2(os.path.join(tmp, extra), os.path.join(APP, extra))
        run(["chown", "-R", "livetiming:livetiming", APP])
        run(["systemctl", "restart", "livetiming"], 30)
        ok = False
        for _ in range(25):
            time.sleep(1)
            try:
                v = json.loads(urllib.request.urlopen("http://127.0.0.1:8080/api/version", timeout=2).read())
                if v.get("version") == ver:
                    ok = True
                    break
            except Exception:
                pass
        if not ok:
            say("nieuwe versie antwoordt niet – terug naar de vorige")
            if os.path.exists(os.path.join(bdir, "livetiming.py")):
                shutil.copy2(os.path.join(bdir, "livetiming.py"), os.path.join(APP, "livetiming.py"))
            if os.path.isdir(os.path.join(bdir, "static")):
                shutil.copytree(os.path.join(bdir, "static"), os.path.join(APP, "static"), dirs_exist_ok=True)
            run(["chown", "-R", "livetiming:livetiming", APP])
            run(["systemctl", "restart", "livetiming"], 30)
            raise RuntimeError("update teruggedraaid (gezondheidscheck mislukt)")
        say(f"live timing draait versie {ver}")
        # de beheerdienst zelf bijwerken (na het antwoord herstarten)
        new_agent = os.path.join(tmp, "cloud", "cloud_agent.py")
        if os.path.exists(new_agent):
            rc, _, se = run([sys.executable, "-m", "py_compile", new_agent], 20)
            cur = os.path.join(AGENT_DIR, "cloud_agent.py")
            if rc == 0 and (not os.path.exists(cur) or open(new_agent, "rb").read() != open(cur, "rb").read()):
                shutil.copy2(new_agent, cur)
                restart("agent")
                say("beheerdienst bijgewerkt (herstart over 1 s)")
        cleanup = sorted(os.listdir(BACKUPS))[:-5]
        for d in cleanup:
            shutil.rmtree(os.path.join(BACKUPS, d), ignore_errors=True)
        return {"ok": True, "version": ver, "log": out}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- console (PTY)
class Session:
    def __init__(self, sid, who):
        self.id, self.who = sid, who
        self.buf = bytearray()
        self.base = 0                     # positie van buf[0] in de totale uitvoer
        self.cond = threading.Condition()
        self.started = self.last = time.time()
        self.closed = False
        env = {"TERM": "xterm-256color", "HOME": "/root", "USER": "root", "LOGNAME": "root", "LANG": "C.UTF-8",
               "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "SHELL": "/bin/bash"}
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir("/root")
            os.execve("/bin/bash", ["bash", "-l"], env)
        self.pid, self.fd = pid, fd
        self.resize(120, 32)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 1.0)
                if not r:
                    if self.closed:
                        break
                    continue
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            with self.cond:
                if not data:
                    self.closed = True
                    self.cond.notify_all()
                    break
                self.buf += data
                if len(self.buf) > 512 * 1024:          # enkel de laatste 256 kB bijhouden
                    cut = len(self.buf) - 256 * 1024
                    del self.buf[:cut]
                    self.base += cut
                self.cond.notify_all()

    def read(self, pos, wait):
        self.last = time.time()
        end = time.time() + wait
        with self.cond:
            while self.base + len(self.buf) <= pos and not self.closed and time.time() < end:
                self.cond.wait(max(0.05, end - time.time()))
            start = max(pos, self.base)
            data = bytes(self.buf[start - self.base:])
            return {"pos": start + len(data), "data": base64.b64encode(data).decode(), "closed": self.closed,
                    "skipped": start > pos}

    def write(self, data):
        self.last = time.time()
        os.write(self.fd, data)

    def resize(self, cols, rows):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(10, min(500, rows)), max(20, min(500, cols)), 0, 0))

    def close(self):
        self.closed = True
        try:
            os.kill(self.pid, signal.SIGHUP)
            time.sleep(0.2)
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, os.WNOHANG)
            os.close(self.fd)
        except OSError:
            pass
        with self.cond:
            self.cond.notify_all()


def console_open(pw, who):
    with LOCK:
        if FAILS["until"] > time.time():
            raise PermissionError(f"console geblokkeerd na te veel foute wachtwoorden (nog {int(FAILS['until'] - time.time())} s)")
        if not load_conf().get("console"):
            raise PermissionError("geen consolewachtwoord ingesteld (op de server: cloud_agent.py --set-password)")
        time.sleep(0.5)
        if not check_pw(pw):
            FAILS["n"] += 1
            log(f"console: fout wachtwoord ({FAILS['n']}) van {who}")
            if FAILS["n"] >= 5:
                FAILS["until"] = time.time() + 900
                FAILS["n"] = 0
            raise PermissionError("verkeerd consolewachtwoord")
        FAILS["n"] = 0
        if len(SESSIONS) >= MAX_SESSIONS:
            raise PermissionError("te veel open consoles – sluit er eerst een")
        sid = secrets.token_urlsafe(24)
        SESSIONS[sid] = Session(sid, who)
        log(f"console geopend door {who}")
        return sid


def reaper():
    while True:
        time.sleep(30)
        now = time.time()
        for sid, s in list(SESSIONS.items()):
            if s.closed or now - s.last > IDLE_S or now - s.started > MAX_S:
                s.close()
                SESSIONS.pop(sid, None)
                log(f"console gesloten ({s.who})")


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if self.headers.get("Origin") or self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return False
        return hmac.compare_digest(self.headers.get("X-Agent-Key", ""), KEY)

    def _body(self, limit=1024 * 1024):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > limit:
            raise ValueError("te groot")
        return self.rfile.read(n) if n else b""

    def _who(self):
        return self.headers.get("X-Who", "?")[:80]

    def do_GET(self):
        if not self._auth():
            return self._send(403, {"ok": False, "error": "geen toegang"})
        path, _, q = self.path.partition("?")
        qs = dict(p.split("=", 1) for p in q.split("&") if "=" in p)
        try:
            if path == "/status":
                return self._send(200, dict(status(), ok=True))
            if path == "/logs":
                return self._send(200, {"ok": True, "text": logs(qs.get("unit", "livetiming"), qs.get("n", 200))})
            if path.startswith("/console/") and path.endswith("/read"):
                s = SESSIONS.get(path.split("/")[2])
                if not s:
                    return self._send(404, {"ok": False, "error": "console bestaat niet (meer)"})
                return self._send(200, dict(s.read(int(qs.get("pos", 0)), min(20.0, float(qs.get("wait", 20)))), ok=True))
        except Exception as e:
            return self._send(400, {"ok": False, "error": str(e)})
        self._send(404, {"ok": False, "error": "onbekend"})

    def do_POST(self):
        if not self._auth():
            return self._send(403, {"ok": False, "error": "geen toegang"})
        path = self.path.split("?")[0]
        try:
            if path == "/update":
                return self._send(200, install_release(self._body(60 * 1024 * 1024)))
            d = json.loads(self._body() or b"{}")
            if path == "/restart":
                return self._send(200, {"ok": restart(d.get("unit", ""))})
            if path == "/console/open":
                sid = console_open(d.get("password", ""), self._who())
                if d.get("cols"):
                    SESSIONS[sid].resize(int(d["cols"]), int(d.get("rows", 30)))
                return self._send(200, {"ok": True, "id": sid})
            if path == "/console/password":
                if load_conf().get("console") and not check_pw(d.get("old", "")):
                    time.sleep(1)
                    raise PermissionError("huidig consolewachtwoord klopt niet")
                new = d.get("new", "")
                if len(new) < 12:
                    raise ValueError("nieuw wachtwoord: minstens 12 tekens")
                c = load_conf()
                c["console"] = hash_pw(new)
                save_conf(c)
                log(f"consolewachtwoord gewijzigd door {self._who()}")
                return self._send(200, {"ok": True})
            if path.startswith("/console/"):
                parts = path.split("/")
                s = SESSIONS.get(parts[2]) if len(parts) > 3 else None
                if not s:
                    return self._send(404, {"ok": False, "error": "console bestaat niet (meer)"})
                act = parts[3]
                if act == "write":
                    s.write(base64.b64decode(d.get("data", "")))
                elif act == "resize":
                    s.resize(int(d.get("cols", 120)), int(d.get("rows", 32)))
                elif act == "close":
                    s.close()
                    SESSIONS.pop(s.id, None)
                    log(f"console gesloten door {s.who}")
                return self._send(200, {"ok": True})
        except PermissionError as e:
            return self._send(403, {"ok": False, "error": str(e)})
        except Exception as e:
            return self._send(400, {"ok": False, "error": str(e)})
        self._send(404, {"ok": False, "error": "onbekend"})


def main():
    global KEY
    if "--set-password" in sys.argv:
        pw = getpass.getpass("Nieuw consolewachtwoord (min. 12 tekens): ")
        if len(pw) < 12 or pw != getpass.getpass("Nog eens: "):
            sys.exit("niet gewijzigd (te kort of niet gelijk)")
        c = load_conf()
        c["console"] = hash_pw(pw)
        save_conf(c)
        print("ingesteld")
        return
    if "--set-password-stdin" in sys.argv:          # voor de installatie (pve_deploy): wachtwoord via stdin
        pw = sys.stdin.readline().strip()
        if len(pw) >= 12:
            c = load_conf()
            c["console"] = hash_pw(pw)
            save_conf(c)
            print("ingesteld")
        return
    KEY = ensure_key()
    os.makedirs(BACKUPS, exist_ok=True)
    threading.Thread(target=reaper, daemon=True).start()
    srv = ThreadingHTTPServer(LISTEN, H)
    srv.daemon_threads = True
    log(f"livetiming-agent luistert op {LISTEN[0]}:{LISTEN[1]}")
    srv.serve_forever()


KEY = ""
if __name__ == "__main__":
    main()
