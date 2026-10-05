"""Zet de publieke relay-server op een LXC-container in Proxmox (via de Proxmox-API, enkel stdlib).

Gebruik (vanuit de projectmap):
    python tools/pve_deploy.py create     container aanmaken + starten (eenmalig)
    python tools/pve_deploy.py deploy     Python + code + dienst installeren / code bijwerken
    python tools/pve_deploy.py status     dienststatus en snelle test

Leest proxmox.env (PVE_URL, PVE_TOKEN_ID, PVE_TOKEN_SECRET, PVE_NODE).
Geheimen voor de relay (relay_token, jury_password) staan in relay/relay.env (gitignored).
Commando's in de container lopen via de Proxmox-console (termproxy/websocket); de console staat
daarvoor tijdelijk op cmode=shell en wordt nadien teruggezet op tty.
"""
import base64
import hashlib
import http.client
import io
import json
import os
import re
import secrets
import socket
import ssl
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CT = {
    "vmid": 133,
    "hostname": "khzs-livetiming",
    "ostemplate": "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
    "rootfs": "rpool:4",
    "memory": 512,
    "swap": 256,
    "cores": 1,
    "net0": "name=eth0,bridge=vmbr1,gw=10.10.30.1,ip=10.10.30.133/24,type=veth",
    "unprivileged": 1,
    "features": "nesting=1",
    "onboot": 1,
    "cmode": "shell",
    "tags": "khzs;livetiming",
    "description": "KHZS live timing – publieke relay (python3 livetiming.py --relay-server, poort 8080).\n"
                   "Code: /opt/livetiming, dienst: livetiming.service. Bijwerken: tools/pve_deploy.py deploy",
}
IP = "10.10.30.133"
FILES = ["livetiming.py", "README.md", "static/index.html", "static/callroom.html", "static/settings.html",
         "static/info.html", "static/infoscreen.js", "static/img/hzs-wordmark.png", "static/img/hzs-wordmark-wit.png"]

UNIT = """[Unit]
Description=KHZS live timing relay
After=network-online.target
Wants=network-online.target

[Service]
User=livetiming
Group=livetiming
WorkingDirectory=/opt/livetiming
ExecStart=/usr/bin/python3 -u /opt/livetiming/livetiming.py --relay-server --bind 0.0.0.0 --http-port 8080 --settings /opt/livetiming/settings.json --no-rawlog
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/opt/livetiming

[Install]
WantedBy=multi-user.target
"""


AGENT_UNIT = """[Unit]
Description=HZS Timing - beheerdienst cloudserver (enkel 127.0.0.1:8092)
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 -u /opt/livetiming-agent/cloud_agent.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""


def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8-sig"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env(os.path.join(ROOT, "proxmox.env"))
U = urllib.parse.urlsplit(ENV["PVE_URL"])
NODE = ENV["PVE_NODE"]
AUTH = f"PVEAPIToken={ENV['PVE_TOKEN_ID']}={ENV['PVE_TOKEN_SECRET']}"
# Proxmox gebruikt een certificaat van de eigen cluster-CA: geen gewone CA-controle mogelijk, daarom
# vastpinnen op de SHA-256-vingerafdruk (PVE_FINGERPRINT in proxmox.env, anders vastgelegd bij eerste gebruik).
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
FP_FILE = os.path.join(ROOT, "relay", "pve_fingerprint.txt")


def check_pin(sock):
    fp = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
    pinned = ENV.get("PVE_FINGERPRINT", "").replace(":", "").lower()
    if not pinned and os.path.exists(FP_FILE):
        pinned = open(FP_FILE, encoding="utf-8").read().strip()
    if not pinned:
        os.makedirs(os.path.dirname(FP_FILE), exist_ok=True)
        open(FP_FILE, "w", encoding="utf-8").write(fp)
        print("certificaat-vingerafdruk Proxmox vastgelegd:", ":".join(fp[i:i + 2] for i in range(0, 64, 2)).upper())
    elif fp != pinned:
        sock.close()
        raise SystemExit("GESTOPT: het certificaat van Proxmox wijkt af van de vastgelegde vingerafdruk "
                         f"({FP_FILE}). Controleer eerst of dit klopt (Node > System > Certificates).")


def api(method, path, data=None, timeout=60):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    conn = http.client.HTTPSConnection(U.hostname, U.port or 8006, context=CTX, timeout=timeout)
    try:
        conn.connect()
        check_pin(conn.sock)
        hdr = {"Authorization": AUTH}
        if body is not None:
            hdr["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, "/api2/json" + path, body=body, headers=hdr)
        r = conn.getresponse()
        raw = r.read()
        if r.status >= 400:
            raise RuntimeError(f"{method} {path}: HTTP {r.status} {r.reason} {raw.decode(errors='replace')[:400]}")
        return json.loads(raw).get("data")
    finally:
        conn.close()


def wait_task(upid, timeout=600):
    t0 = time.time()
    q = urllib.parse.quote(upid, safe="")
    while time.time() - t0 < timeout:
        st = api("GET", f"/nodes/{NODE}/tasks/{q}/status")
        if st.get("status") == "stopped":
            if st.get("exitstatus") != "OK":
                log = api("GET", f"/nodes/{NODE}/tasks/{q}/log?limit=50")
                raise RuntimeError("taak mislukt: " + str(st.get("exitstatus")) + "\n" +
                                   "\n".join(x.get("t", "") for x in log))
            return
        time.sleep(2)
    raise TimeoutError(upid)


def relay_secrets():
    os.makedirs(os.path.join(ROOT, "relay"), exist_ok=True)
    p = os.path.join(ROOT, "relay", "relay.env")
    env = load_env(p)
    changed = False
    if not env.get("RELAY_TOKEN"):
        env["RELAY_TOKEN"] = secrets.token_hex(24); changed = True    # enkel 0-9a-f: veilig te kopiëren
    if not env.get("CLOUD_ADMIN_PASSWORD"):     # beheer op de cloudserver zelf (infoscherm, agenda, kijkers)
        env["CLOUD_ADMIN_PASSWORD"] = secrets.token_urlsafe(12); changed = True
    if not env.get("CONSOLE_PASSWORD"):         # root-console via het beheer (apart van het relay-token)
        env["CONSOLE_PASSWORD"] = secrets.token_urlsafe(15); changed = True
    if not env.get("JURY_PASSWORD"):
        a = "abcdefghjkmnpqrstuvwxyz23456789"
        env["JURY_PASSWORD"] = "zwem-" + "".join(secrets.choice(a) for _ in range(4)) + "-" + \
                               "".join(secrets.choice(a) for _ in range(4)); changed = True
    if changed:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("# Geheimen van de publieke relay (NIET in git). Zelfde RELAY_TOKEN op de laptop (/settings > Doorsturen).\n")
            for k in ("RELAY_TOKEN", "JURY_PASSWORD", "CONSOLE_PASSWORD", "CLOUD_ADMIN_PASSWORD"):
                f.write(f"{k}={env[k]}\n")
    return env


# ---------------------------------------------------------------- websocket-console
class Console:
    """Minimale websocket-client voor de Proxmox termproxy (xterm.js-protocol)."""

    def __init__(self, vmid):
        tp = api("POST", f"/nodes/{NODE}/lxc/{vmid}/termproxy", {})
        path = (f"/api2/json/nodes/{NODE}/lxc/{vmid}/vncwebsocket?port={tp['port']}"
                f"&vncticket={urllib.parse.quote(tp['ticket'], safe='')}")
        raw = socket.create_connection((U.hostname, U.port or 8006), timeout=30)
        self.s = CTX.wrap_socket(raw, server_hostname=U.hostname)
        check_pin(self.s)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {U.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: binary\r\n"
               f"Authorization: {AUTH}\r\n\r\n")
        self.s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            c = self.s.recv(4096)
            if not c:
                raise RuntimeError("websocket gesloten tijdens handshake")
            resp += c
        head, _, self.rbuf = resp.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("websocket geweigerd: " + head.decode(errors="replace")[:300])
        self.s.settimeout(None)
        self.out = ""
        self.cv = threading.Condition()
        self.closed = False
        self.wlock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()
        self._frame(f"{tp['user']}:{tp['ticket']}\n".encode())
        self.wait_for(lambda o: "OK" in o, 20, "termproxy-login")
        self._frame(b"1:220:50:")                      # terminalgrootte
        threading.Thread(target=self._keepalive, daemon=True).start()
        self.n = 0
        time.sleep(1.0)
        self.send("export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root "
                  "TERM=dumb DEBIAN_FRONTEND=noninteractive PS1='' PS2=''; stty -echo\n")
        time.sleep(0.8)
        self.run("true", 20)

    def _frame(self, payload, opcode=2):
        hdr = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += n.to_bytes(2, "big")
        else:
            hdr.append(0x80 | 127); hdr += n.to_bytes(8, "big")
        mask = os.urandom(4)
        hdr += mask
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.wlock:
            self.s.sendall(bytes(hdr) + data)

    def _read(self, n):
        while len(self.rbuf) < n:
            c = self.s.recv(65536)
            if not c:
                raise EOFError
            self.rbuf += c
        d, self.rbuf = self.rbuf[:n], self.rbuf[n:]
        return d

    def _reader(self):
        try:
            while True:
                b0, b1 = self._read(2)
                op, n = b0 & 0x0F, b1 & 0x7F
                if n == 126:
                    n = int.from_bytes(self._read(2), "big")
                elif n == 127:
                    n = int.from_bytes(self._read(8), "big")
                mask = self._read(4) if b1 & 0x80 else None
                data = self._read(n)
                if mask:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                if op == 8:
                    break
                if op == 9:
                    self._frame(data, 10); continue
                if op in (0, 1, 2):
                    with self.cv:
                        self.out += data.decode("utf-8", "replace")
                        self.cv.notify_all()
        except (EOFError, OSError):
            pass
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def _keepalive(self):
        while not self.closed:
            time.sleep(20)
            try:
                self._frame(b"2")
            except OSError:
                return

    def send(self, text):
        b = text.encode("utf-8")
        self._frame(f"0:{len(b)}:".encode() + b)

    def wait_for(self, pred, timeout, what):
        t0 = time.time()
        with self.cv:
            while not pred(self.out):
                if self.closed:
                    raise RuntimeError(f"console gesloten tijdens {what}; uitvoer:\n{self.out[-1500:]}")
                left = timeout - (time.time() - t0)
                if left <= 0:
                    raise TimeoutError(f"{what}: geen antwoord; laatste uitvoer:\n{self.out[-1500:]}")
                self.cv.wait(left)

    def run(self, cmd, timeout=120, check=True):
        self.n += 1
        tag = f"__END_{self.n}__:"
        with self.cv:
            start = len(self.out)
        self.send(f"{cmd}\necho \"__E\"\"ND_{self.n}__:$?\"\n")
        rx = re.compile(re.escape(tag) + r"(\d+)")
        self.wait_for(lambda o: rx.search(o, start), timeout, cmd.splitlines()[0][:60])
        with self.cv:
            m = rx.search(self.out, start)
            text = self.out[start:m.start()]
        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r", "", text).strip()
        code = int(m.group(1))
        if check and code != 0:
            raise RuntimeError(f"commando mislukt ({code}): {cmd[:200]}\n{text[-1500:]}")
        return code, text

    def put_file(self, data, dest, mode="644", chunk_lines=100):
        b64 = base64.encodebytes(data).decode().splitlines()
        self.run(f": > /tmp/_up.b64")
        for i in range(0, len(b64), chunk_lines):
            body = "\n".join(b64[i:i + chunk_lines])
            self.run(f"cat >> /tmp/_up.b64 <<'B64EOF'\n{body}\nB64EOF", 60)
        want = hashlib.sha256(data).hexdigest()
        _, got = self.run(f"base64 -d /tmp/_up.b64 > {dest} && chmod {mode} {dest} && sha256sum {dest} | cut -d' ' -f1"
                          f" && rm -f /tmp/_up.b64")
        if want not in got:
            raise RuntimeError(f"controlesom klopt niet voor {dest}: {got[-200:]}")

    def close(self):
        try:
            self.send("exit\n")
            self._frame(b"", 8)
            self.s.close()
        except OSError:
            pass


# ---------------------------------------------------------------- stappen
def ct_exists():
    return any(int(c["vmid"]) == CT["vmid"] for c in api("GET", f"/nodes/{NODE}/lxc"))


def ct_status():
    return api("GET", f"/nodes/{NODE}/lxc/{CT['vmid']}/status/current").get("status")


def set_cmode(mode):
    api("PUT", f"/nodes/{NODE}/lxc/{CT['vmid']}/config", {"cmode": mode})


def cmd_create():
    if ct_exists():
        print(f"container {CT['vmid']} bestaat al – niets aangemaakt")
    else:
        if str(api("GET", "/cluster/nextid")) != str(CT["vmid"]) and \
                any(int(c["vmid"]) == CT["vmid"] for c in api("GET", "/cluster/resources?type=vm")):
            raise SystemExit(f"VMID {CT['vmid']} is in gebruik")
        for c in api("GET", f"/nodes/{NODE}/lxc"):
            cfg = api("GET", f"/nodes/{NODE}/lxc/{c['vmid']}/config")
            if f"ip={IP}/" in (cfg.get("net0") or ""):
                raise SystemExit(f"IP {IP} wordt al gebruikt door container {c['vmid']}")
        print(f"container {CT['vmid']} ({CT['hostname']}, {IP}) aanmaken…")
        wait_task(api("POST", f"/nodes/{NODE}/lxc", CT), 900)
        print("aangemaakt")
    if ct_status() != "running":
        wait_task(api("POST", f"/nodes/{NODE}/lxc/{CT['vmid']}/status/start", {}))
        print("gestart")
        time.sleep(5)


def build_tar():
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tar:
        for rel in FILES:
            data = open(os.path.join(ROOT, rel), "rb").read()
            ti = tarfile.TarInfo(rel)
            ti.size, ti.mode, ti.mtime = len(data), 0o644, int(time.time())
            tar.addfile(ti, io.BytesIO(data))
    return bio.getvalue()


def cmd_deploy():
    if ct_status() != "running":
        raise SystemExit("container draait niet – eerst: python tools/pve_deploy.py create")
    sec = relay_secrets()
    set_cmode("shell")
    con = None
    try:
        con = Console(CT["vmid"])
        print("console verbonden")
        _, osr = con.run("head -1 /etc/os-release; hostname -I")
        print("  ", osr.replace("\n", " | "))
        if con.run("command -v python3", check=False)[0] != 0 or \
                con.run("dpkg -s unattended-upgrades >/dev/null 2>&1", check=False)[0] != 0:
            print("pakketten installeren (python3, unattended-upgrades)…")
            con.run("apt-get -qq -o Acquire::ForceIPv4=true update && apt-get -qq -o Acquire::ForceIPv4=true -y install "
                    "python3 unattended-upgrades curl >/dev/null && echo 'Acquire::ForceIPv4 \"true\";' > /etc/apt/apt.conf.d/99force-ipv4", 600)
        print("  ", con.run("python3 --version")[1])
        con.run("printf 'net.ipv6.conf.all.disable_ipv6=1\nnet.ipv6.conf.default.disable_ipv6=1\n' "
                "> /etc/sysctl.d/90-geen-ipv6.conf && sysctl -q -p /etc/sysctl.d/90-geen-ipv6.conf; "
                "grep -q '^precedence ::ffff:0:0/96' /etc/gai.conf 2>/dev/null || echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf",
                check=False)
        con.run("ln -sf /usr/share/zoneinfo/Europe/Brussels /etc/localtime && echo Europe/Brussels > /etc/timezone")
        con.run("id livetiming >/dev/null 2>&1 || useradd --system --home-dir /opt/livetiming --no-create-home "
                "--shell /usr/sbin/nologin livetiming; mkdir -p /opt/livetiming")
        tar = build_tar()
        print(f"code uploaden ({len(tar)//1024} KB)…")
        con.put_file(tar, "/tmp/livetiming.tgz")
        con.run("tar -xzf /tmp/livetiming.tgz -C /opt/livetiming && rm -f /tmp/livetiming.tgz")
        settings = json.dumps({"relay_token": sec["RELAY_TOKEN"], "jury_password": sec["JURY_PASSWORD"],
                               "cloud_admin_password": sec["CLOUD_ADMIN_PASSWORD"]}, indent=1)
        con.run("test -f /opt/livetiming/settings.json || touch /opt/livetiming/settings.json")
        con.run("python3 - <<'PYEOF'\nimport json\np='/opt/livetiming/settings.json'\n"
                "try:\n    d=json.load(open(p,encoding='utf-8-sig'))\nexcept Exception:\n    d={}\n"
                f"d.update({{k: v for k, v in json.loads({settings!r}).items() if k != 'cloud_admin_password' or not d.get(k)}})\njson.dump(d,open(p,'w',encoding='utf-8'),indent=1)\nPYEOF")
        con.run("chown -R livetiming:livetiming /opt/livetiming && chmod 600 /opt/livetiming/settings.json")
        con.run(f"cat > /etc/systemd/system/livetiming.service <<'UNITEOF'\n{UNIT}UNITEOF")
        # beheerdienst (root, enkel localhost): status, logboek, herstarten, updates, console
        con.run("mkdir -p /opt/livetiming-agent /opt/livetiming-backups && chmod 700 /opt/livetiming-agent")
        con.put_file(open(os.path.join(ROOT, "cloud", "cloud_agent.py"), "rb").read(), "/opt/livetiming-agent/cloud_agent.py", "700")
        con.run(f"cat > /etc/systemd/system/livetiming-agent.service <<'UNITEOF'\n{AGENT_UNIT}UNITEOF")
        if con.run("python3 -c \"import json;exit(0 if json.load(open('/etc/livetiming-agent.json')).get('console') else 1)\" 2>/dev/null",
                   check=False)[0] != 0:
            con.run(f"printf '%s\\n' '{sec['CONSOLE_PASSWORD']}' | python3 /opt/livetiming-agent/cloud_agent.py --set-password-stdin")
            print("consolewachtwoord ingesteld (zie relay/relay.env)")
        con.run("systemctl daemon-reload && systemctl enable livetiming-agent >/dev/null 2>&1 && systemctl restart livetiming-agent")
        con.run("systemctl daemon-reload && systemctl enable livetiming >/dev/null 2>&1 && systemctl restart livetiming")
        time.sleep(3)
        code, st = con.run("systemctl is-active livetiming", check=False)
        if st.strip() != "active":
            print("dienst start niet met sandboxing; opnieuw zonder Protect*-opties")
            con.run("sed -i '/^Protect\\|^PrivateTmp\\|^ReadWritePaths/d' /etc/systemd/system/livetiming.service && "
                    "systemctl daemon-reload && systemctl restart livetiming")
            time.sleep(3)
        print("dienst:", con.run("systemctl is-active livetiming", check=False)[1])
        print(con.run("journalctl -u livetiming -n 8 --no-pager -o cat", check=False)[1])
        _test(con, sec)
    finally:
        if con:
            con.close()
        set_cmode("tty")
        print("console terug op tty")


def _test(con, sec):
    t = sec["RELAY_TOKEN"]
    script = f"""python3 - <<'PYEOF'
import json, urllib.request
b='http://127.0.0.1:8080'
def get(p):
    try:
        r=urllib.request.urlopen(urllib.request.Request(b+p), timeout=5); return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, b''
class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k): return None
op=urllib.request.build_opener(NR)
try: op.open(b+'/jury', timeout=5); jr='200 (FOUT: geen login gevraagd)'
except urllib.error.HTTPError as e: jr=str(e.code)+' -> '+str(e.headers.get('Location'))
s,d=get('/state'); st=json.loads(d)
print('publiek /state', s, '| relay', st.get('relay'), '| connected', st.get('connected'))
print('/jury zonder login', jr)
print('/api/callroom zonder login', get('/api/callroom')[0])
req=urllib.request.Request(b+'/ingest', data=b'{{"items":[]}}', headers={{'X-Ingest-Token':'{t}','Content-Type':'application/json'}})
print('ingest met token', urllib.request.urlopen(req, timeout=5).read().decode())
try:
    urllib.request.urlopen(urllib.request.Request(b+'/ingest', data=b'{{"items":[]}}', headers={{'X-Ingest-Token':'fout'}}), timeout=5); print('ingest fout token: 200 (FOUT)')
except urllib.error.HTTPError as e: print('ingest fout token', e.code)
r=urllib.request.urlopen(urllib.request.Request(b+'/admin/status', headers={{'X-Ingest-Token':'{t}'}}), timeout=10)
st=json.loads(r.read()); print('beheer /admin/status', r.status, '| versie', st.get('version'), '| diensten', st.get('units'), '| console', st.get('console'))
try:
    urllib.request.urlopen(urllib.request.Request(b+'/admin/status', headers={{'X-Ingest-Token':'fout'}}), timeout=10); print('beheer fout token: 200 (FOUT)')
except urllib.error.HTTPError as e: print('beheer fout token', e.code)
PYEOF"""
    print(con.run(script, 30, check=False)[1])


def cmd_user():
    """Gebruiker met sudo-rechten aanmaken op de cloudserver: python tools/pve_deploy.py user NAAM
    Het wachtwoord komt in relay/users.env (niet in git); bij de eerste login vraagt sudo dat wachtwoord."""
    name = sys.argv[2] if len(sys.argv) > 2 else ""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,30}", name):
        raise SystemExit("gebruik: python tools/pve_deploy.py user NAAM  (kleine letters/cijfers)")
    p = os.path.join(ROOT, "relay", "users.env")
    env = load_env(p)
    key = f"{name.upper().replace('-', '_')}_PASSWORD"
    if not env.get(key):
        env[key] = secrets.token_urlsafe(14)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("# Gebruikers op de cloudserver (NIET in git)\n")
            for k, v in env.items():
                f.write(f"{k}={v}\n")
    set_cmode("shell")
    con = None
    try:
        con = Console(CT["vmid"])
        if con.run("command -v sudo", check=False)[0] != 0:
            print("sudo installeren…")
            con.run("apt-get -qq -o Acquire::ForceIPv4=true update && apt-get -qq -o Acquire::ForceIPv4=true -y install sudo >/dev/null", 600)
        con.run(f"id {name} >/dev/null 2>&1 || useradd -m -s /bin/bash {name}")
        con.run(f"usermod -aG sudo {name}")
        con.put_file(f"{name}:{env[key]}\n".encode(), "/root/.newpw", "600")
        con.run("chpasswd < /root/.newpw; rm -f /root/.newpw")
        print(con.run(f"id {name}; sudo -l -U {name} | tail -2", check=False)[1])
        print(f"gebruiker {name} klaar (sudo). Wachtwoord: relay/users.env ({key})")
    finally:
        if con:
            con.close()
        set_cmode("tty")


def cmd_status():
    print("container:", ct_status())
    sec = relay_secrets()
    set_cmode("shell")
    con = None
    try:
        con = Console(CT["vmid"])
        print("dienst:", con.run("systemctl is-active livetiming", check=False)[1])
        print(con.run("journalctl -u livetiming -n 15 --no-pager -o cat", check=False)[1])
        _test(con, sec)
    finally:
        if con:
            con.close()
        set_cmode("tty")


if __name__ == "__main__":
    {"create": cmd_create, "deploy": cmd_deploy, "status": cmd_status, "user": cmd_user}[sys.argv[1] if len(sys.argv) > 1 else "status"]()
