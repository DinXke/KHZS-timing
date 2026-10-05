#!/usr/bin/env python3
"""KHZS Timing – systeemdienst voor de Raspberry Pi (draait als root, luistert enkel op 127.0.0.1).

Taken:
  * netwerk: ethernet (DHCP / vast / delen met NAT) en Wi-Fi-client via NetworkManager (nmcli)
  * hotspot "KHZS-Timing": automatisch na 60 s zonder netwerk, of altijd aan; captive portal naar /system
  * HDMI-scherm: Chromium-kiosk met instelbare pagina
  * hotspot-login: een browser op de Pi (headless Chromium) die je vanuit het beheer bedient
  * updates: zip uploaden of automatisch van GitHub, met zelftest en automatische terugval
  * herstarten / uitschakelen, status (IP's, temperatuur, opslag, ...)

De live-timingserver (livetiming.py) controleert de beheerderscode en stuurt /api/system/* hierheen door.
Enkel standaardbibliotheek van Python.
"""
import base64
import glob
import fcntl
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = ("127.0.0.1", 8091)
DATA = "/var/lib/khzs"
CONF = os.path.join(DATA, "system.json")
KIOSK_ENV = os.path.join(DATA, "kiosk.env")
RELEASES = "/opt/khzs/releases"
CURRENT = "/opt/khzs/current"
APP_PORT = 80
AP_CON = "khzs-ap"
ETH_CON = "khzs-eth"
CAPTIVE_CONF = "/etc/NetworkManager/dnsmasq-shared.d/khzs-captive.conf"
ROLE_ENV = os.path.join(DATA, "role.env")
KEY_FILE = os.path.join(DATA, "agent.key")
DEBUG = os.environ.get("KHZS_DEBUG") == "1" or os.path.exists(os.path.join(DATA, "debug.env"))
AGENT_KEY = ""
FAILS = os.path.join(DATA, "agent-fails")
BOOT_FILE = "/boot/firmware/khzs-instellingen.txt"
SD_HASH = os.path.join(DATA, "sdfile.sha256")
APPLY = os.path.join(CURRENT, "appliance", "system", "apply.sh")

DEFAULTS = {
    "hotspot": {"ssid": "KHZS-Timing", "password": "zwemclub", "always": False, "after_s": 60, "ip": "10.42.0.1"},
    "ethernet": {"mode": "dhcp", "address": "", "gateway": "", "dns": ""},     # oud formaat (migratie)
    "network": {
        "wan": "auto",                                   # auto | ethernet | wifi | usb
        "ethernet": {"role": "wan", "ipv4": "dhcp", "address": "", "gateway": "", "dns": ""},   # role: wan | lan | off
        "wifi": {"role": "client"},                      # client | hotspot | client+hotspot | off
        "usb": {"enabled": True},                        # 4G-router / USB-tethering als WAN
        "fallback": True,                                # noodhotspot: kan NIET uit (vangnet tegen buitensluiten)
        "internet": True,                                # internet toestaan (standaardroute via WAN)
        "lan": {
            "ethernet": {"address": "10.43.0.1/24", "dhcp_start": "10.43.0.100", "dhcp_end": "10.43.0.199",
                         "lease_h": 12, "dns": ""},
            "hotspot": {"address": "10.42.0.1/24", "dhcp_start": "10.42.0.100", "dhcp_end": "10.42.0.199",
                        "lease_h": 4, "dns": ""},
        },
        "reservations": [],                              # vaste adressen: {mac, ip, name} (bv. de SwimTime-pc)
    },
    "kiosk": {"enabled": True, "url": "http://localhost/jury", "zoom": 1.0, "mode": "auto", "rotate": 0},
    "device": {"name": "khzs-timing", "role": "uit", "display_url": "https://timing.khzs.be/callroom",
               "display_key": ""},
    "update": {"auto": False, "repo": "DinXke/KHZS-timing", "token": "", "idle_min": 15},
    "shares": [],     # netwerkschijven: {name, path, user, password, domain, vers, auto} – altijd alleen-lezen
}

LOCK = threading.RLock()
STATE = {"since_uplink": time.time(), "uplink": False, "internet": None, "ap_iface": None, "ap_reason": "",
         "last_retry": 0.0, "update": {"busy": False, "log": [], "last": None, "available": None},
         "wifi_job": None, "boot": time.time(), "connectivity": None, "sd_errors": [],
         "system": {"pending": None, "busy": False, "log": [], "checked": None},
         "confirm": None}                                  # netwerkwijziging die nog bevestigd moet worden


# ---------------------------------------------------------------- hulpfuncties
def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def run(cmd, timeout=30, check=False, input=None):
    if DEBUG:
        log("$ " + " ".join(str(c) for c in cmd)[:300])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)
    except (OSError, subprocess.TimeoutExpired) as e:
        if check:
            raise RuntimeError(f"{cmd[0]}: {e}")
        return 1, "", str(e)
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:400] or f"{cmd[0]} faalde")
    return r.returncode, r.stdout, r.stderr


def nm_split(line):
    """nmcli -t splitst op ':' met '\\:' als escape."""
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def nmcli(*args, timeout=30, check=False):
    return run(["nmcli", "-t", *args], timeout=timeout, check=check)


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_conf():
    c = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONF, encoding="utf-8") as f:
            d = json.load(f)
        if "network" not in d and "ethernet" in d:           # migratie van het oude formaat
            m = d["ethernet"].get("mode", "dhcp")
            d["network"] = {"ethernet": {"role": "lan" if m == "share" else "wan",
                                         "ipv4": "static" if m == "static" else "dhcp",
                                         "address": d["ethernet"].get("address", ""),
                                         "gateway": d["ethernet"].get("gateway", ""), "dns": d["ethernet"].get("dns", "")}}
            if d.get("hotspot", {}).get("always"):
                d["network"]["wifi"] = {"role": "client+hotspot"}
        _merge(c, d)
    except (OSError, ValueError):
        pass
    c["network"]["fallback"] = True                       # nooit uitschakelbaar
    c["hotspot"]["ip"] = c["network"]["lan"]["hotspot"]["address"].split("/")[0]
    c["hotspot"]["always"] = c["network"]["wifi"]["role"] in ("hotspot", "client+hotspot")
    return c


def save_conf(c):
    os.makedirs(DATA, exist_ok=True)
    tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONF)
    try:
        write_sd_file(c)
    except Exception as e:
        log(f"SD-bestand: {e}")


def ifaces():
    return sorted(os.path.basename(p) for p in glob.glob("/sys/class/net/*") if os.path.basename(p) != "lo")


def wifi_ifaces():
    return [i for i in ifaces() if os.path.isdir(f"/sys/class/net/{i}/wireless") and not i.startswith("uap")]


def carrier(i):
    try:
        return open(f"/sys/class/net/{i}/carrier").read().strip() == "1"
    except OSError:
        return False


def ip_addrs():
    rc, out, _ = run(["ip", "-j", "-4", "addr"])
    res = {}
    try:
        for d in json.loads(out or "[]"):
            res[d["ifname"]] = [a["local"] + "/" + str(a["prefixlen"]) for a in d.get("addr_info", [])]
    except ValueError:
        pass
    return res


def default_route():
    rc, out, _ = run(["ip", "-j", "-4", "route", "show", "default"])
    try:
        r = json.loads(out or "[]")
        return r[0] if r else None
    except ValueError:
        return None


def devices():
    rc, out, _ = nmcli("-f", "DEVICE,TYPE,STATE,CONNECTION", "device")
    res = []
    for line in out.splitlines():
        p = nm_split(line)
        if len(p) >= 4:
            res.append({"device": p[0], "type": p[1], "state": p[2], "connection": p[3]})
    return res


def active_wifi():
    """Huidige Wi-Fi-clientverbinding (niet de hotspot)."""
    rc, out, _ = nmcli("-f", "ACTIVE,SSID,SIGNAL,CHAN,DEVICE", "device", "wifi", "list", "--rescan", "no")
    for line in out.splitlines():
        p = nm_split(line)
        if len(p) >= 5 and p[0] == "yes":
            return {"ssid": p[1], "signal": int(p[2] or 0), "channel": p[3], "device": p[4]}
    return None


def ap_active():
    for d in devices():
        if d["connection"] == AP_CON and d["state"].startswith("connected"):
            return d["device"]
    return None


def ap_clients(dev):
    if not dev:
        return 0
    rc, out, _ = run(["iw", "dev", dev, "station", "dump"])
    return out.count("Station ")


def uplink():
    """Heeft de Pi een netwerk (niet via de eigen hotspot)? Ethernet, Wi-Fi-client of een USB-modem
    (4G-router, gsm met USB-tethering: verschijnt als extra ethernetkaart, bv. usb0 of enx...)."""
    n = load_conf()["network"]
    for d in devices():
        if d["connection"] in (AP_CON, "", "--") or not d["state"].startswith("connected"):
            continue
        if d["type"] in ("ethernet", "wifi"):
            if d["device"] == "eth0" and n["ethernet"]["role"] != "wan":
                continue                      # de ethernetpoort deelt zelf uit (LAN) of staat uit
            if d["type"] == "ethernet" and d["device"] != "eth0" and not n["usb"]["enabled"]:
                continue
            return d["device"]
    return None


def usb_modems():
    """Extra ethernetkaarten via USB (4G-router, USB-tethering)."""
    return [d for d in devices() if d["type"] == "ethernet" and d["device"] != "eth0"]


def internet_ok():
    for host in (("1.1.1.1", 53), ("8.8.8.8", 53)):
        try:
            with socket.create_connection(host, timeout=2):
                return True
        except OSError:
            pass
    return False


def read_temp():
    try:
        return round(int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000, 1)
    except (OSError, ValueError):
        return None


def throttled():
    rc, out, _ = run(["vcgencmd", "get_throttled"], timeout=5)
    m = re.search(r"0x([0-9a-fA-F]+)", out or "")
    if not m:
        return None
    v = int(m.group(1), 16)
    return {"raw": hex(v), "undervoltage": bool(v & 0x1), "undervoltage_seen": bool(v & 0x10000),
            "throttled": bool(v & 0x4), "throttled_seen": bool(v & 0x40000)}


def app_version(path=CURRENT):
    try:
        m = re.search(r'^VERSION = "([^"]+)"', open(os.path.join(path, "livetiming.py"), encoding="utf-8").read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


# ---------------------------------------------------------------- hotspot
def write_captive(on, ip):
    try:
        if on:
            os.makedirs(os.path.dirname(CAPTIVE_CONF), exist_ok=True)
            with open(CAPTIVE_CONF, "w") as f:
                f.write(f"# KHZS: alle namen naar het beheer zolang er geen netwerk is\naddress=/#/{ip}\n")
        elif os.path.exists(CAPTIVE_CONF):
            os.remove(CAPTIVE_CONF)
    except OSError as e:
        log(f"captive: {e}")


def ensure_uap0(base="wlan0"):
    if os.path.exists("/sys/class/net/uap0"):
        return "uap0"
    rc, _, err = run(["iw", "dev", base, "interface", "add", "uap0", "type", "__ap"])
    if rc != 0:
        log(f"uap0 aanmaken faalde: {err.strip()}")
        return None
    time.sleep(1.5)
    return "uap0" if os.path.exists("/sys/class/net/uap0") else None


def shared_args(lan):
    """NetworkManager 'shared' = DHCP-server + NAT. Bereik en leasetijd instelbaar (NM 1.42+)."""
    a = ["ipv4.addresses", lan["address"]]
    if lan.get("dhcp_start") and lan.get("dhcp_end"):
        a += ["ipv4.shared-dhcp-range", f"{lan['dhcp_start']},{lan['dhcp_end']}"]
    if lan.get("lease_h"):
        a += ["ipv4.shared-dhcp-lease-time", str(int(float(lan["lease_h"]) * 3600))]
    return a


def start_ap(reason):
    """Kiest de juiste radio: USB-stick (wlan1), virtuele uap0 naast een Wi-Fi-client, of wlan0."""
    c = load_conf()["hotspot"]
    wl = wifi_ifaces()
    if not wl:
        return False
    cur = active_wifi()
    if len(wl) > 1:
        dev = [i for i in wl if i != "wlan0"][0]
        channel = None
    elif load_conf()["network"]["wifi"]["role"] == "hotspot":
        if cur:
            nmcli("device", "disconnect", "wlan0")
        dev, channel = "wlan0", None
    elif cur and cur.get("device") == "wlan0":
        dev = ensure_uap0("wlan0")
        channel = cur.get("channel")
        if not dev:
            return False
    else:
        dev = "wlan0"
        channel = None
    has_up = bool(uplink())
    write_captive(not has_up, c["ip"])
    nmcli("connection", "delete", AP_CON)
    args = ["connection", "add", "type", "wifi", "ifname", dev, "con-name", AP_CON, "autoconnect", "no",
            "ssid", c["ssid"], "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
            "ipv4.method", "shared", "ipv6.method", "disabled"] + shared_args(load_conf()["network"]["lan"]["hotspot"])
    if channel:
        args += ["802-11-wireless.channel", str(channel)]
    if c.get("password"):
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", c["password"], "wifi-sec.proto", "rsn",
                 "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp"]
    rc, _, err = nmcli(*args)
    if rc != 0:
        log(f"hotspot aanmaken faalde: {err.strip()}")
        return False
    rc, _, err = nmcli("connection", "up", AP_CON, timeout=40)
    if rc != 0:
        log(f"hotspot starten faalde: {err.strip()}")
        return False
    with LOCK:
        STATE["ap_iface"], STATE["ap_reason"] = dev, reason
    log(f"hotspot {c['ssid']} actief op {dev} ({reason})")
    return True


def stop_ap():
    nmcli("connection", "down", AP_CON, timeout=20)
    write_captive(False, "")
    if os.path.exists("/sys/class/net/uap0"):
        run(["iw", "dev", "uap0", "del"])
    with LOCK:
        STATE["ap_iface"], STATE["ap_reason"] = None, ""
    log("hotspot uit")


def saved_wifi():
    rc, out, _ = nmcli("-f", "NAME,TYPE,AUTOCONNECT-PRIORITY", "connection", "show")
    res = []
    for line in out.splitlines():
        p = nm_split(line)
        if len(p) >= 2 and p[1] == "802-11-wireless" and p[0] != AP_CON:
            res.append(p[0])
    return res


def ensure_retries():
    """NetworkManager geeft standaard na 4 mislukte pogingen een verbinding een tijd op. Hier: altijd blijven proberen."""
    rc, out, _ = nmcli("-f", "NAME,TYPE", "connection", "show")
    for line in out.splitlines():
        p = nm_split(line)
        if len(p) >= 2 and p[1] in ("802-11-wireless", "802-3-ethernet") and p[0] != AP_CON:
            _, cur, _ = nmcli("-g", "connection.autoconnect-retries", "connection", "show", p[0])
            if cur.strip() not in ("0",):
                nmcli("connection", "modify", p[0], "connection.autoconnect-retries", "0")


def netwatch():
    """Elke 5 s: netwerk? Na 'after_s' zonder netwerk de hotspot starten; met netwerk de noodhotspot weer stoppen.
    Komt er een verbinding bij of valt er een weg (USB-4G ingeplugd, kabel, Wi-Fi), dan de routevoorkeur opnieuw
    toepassen: ethernet > Wi-Fi > USB-4G (of de gekozen WAN); zonder internet zakt een verbinding vanzelf weg."""
    last_inet = 0.0
    last_sig = None
    last_retries = 0.0
    down_since = None
    while True:
        try:
            conf = load_conf()
            c = conf["hotspot"]
            net = conf["network"]
            up = uplink()
            now = time.time()
            with LOCK:
                if up:
                    STATE["since_uplink"] = now
                STATE["uplink"] = bool(up)
            if now - last_inet > 30:
                STATE["internet"] = internet_ok() if up else False
                rc, out, _ = nmcli("networking", "connectivity", "check", timeout=20)
                STATE["connectivity"] = (out or "").strip() or None     # full | limited | portal | none | unknown
                last_inet = now
            sig = tuple(sorted((d["device"], d["connection"]) for d in devices()      # enkel wat verbonden is
                               if d["type"] in ("ethernet", "wifi") and d["connection"] != AP_CON
                               and d["state"].startswith("connected") and not d["state"].startswith("connected (")))
            if now - last_retries > 600:
                last_retries = now
                ensure_retries()
            # al 2 minuten geen enkele verbinding: bekende verbindingen zelf opnieuw activeren (naast NetworkManager)
            if up:
                down_since = None
            elif down_since is None:
                down_since = now
            elif now - down_since > 120 and not STATE.get("wifi_job") and not STATE.get("confirm"):
                down_since = now
                for d in devices():
                    if d["type"] == "ethernet" and d["state"].startswith("disconnected"):
                        nmcli("device", "connect", d["device"], timeout=20)
                if ap_active() != "wlan0" and saved_wifi():
                    nmcli("device", "connect", "wlan0", timeout=30)
                log("geen verbinding: bekende verbindingen opnieuw geprobeerd")
            if sig != last_sig and not STATE.get("wifi_job") and not STATE.get("confirm"):
                if last_sig is not None:
                    log("netwerk gewijzigd: " + (", ".join(f"{d} ({c})" for d, c in sig) or "geen verbinding"))
                    apply_usb(net)
                    apply_wan(net)
                last_sig = sig
            ap = ap_active()
            cf = STATE.get("confirm")
            if cf and now > cf["deadline"]:
                network_revert("niet bevestigd binnen 2 minuten")
            if STATE.get("wifi_job"):
                pass                                            # beheer is aan het verbinden: niets forceren
            elif c.get("always"):
                if not ap:
                    start_ap("altijd aan")
            elif net.get("fallback", True) and not up and not ap \
                    and now - STATE["since_uplink"] >= float(c.get("after_s", 60)):
                start_ap("geen netwerk")
            elif ap and not c.get("always") and STATE.get("ap_reason") not in ("geen netwerk", "handmatig"):
                stop_ap()                                       # rol gewijzigd naar 'client' of 'uit'
            elif ap and up and STATE.get("ap_reason") == "geen netwerk" and ap_clients(ap) == 0:
                stop_ap()                                       # netwerk is terug en niemand gebruikt de noodhotspot
            elif ap == "wlan0" and not up and saved_wifi() and ap_clients(ap) == 0 \
                    and now - STATE["last_retry"] > 180:
                # noodhotspot zonder gebruikers: af en toe opnieuw proberen met bekende Wi-Fi-netwerken
                STATE["last_retry"] = now
                log("opnieuw proberen met bekende Wi-Fi-netwerken")
                stop_ap()
                t0 = time.time()
                while time.time() - t0 < 40 and not uplink():
                    time.sleep(3)
                if not uplink():
                    start_ap("geen netwerk")
        except Exception as e:                                 # de bewaking mag nooit stoppen
            log(f"netwatch: {e}")
        time.sleep(5)


# ---------------------------------------------------------------- Wi-Fi / ethernet
def wifi_scan():
    dev = "wlan0"
    rc, out, _ = nmcli("-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN", "device", "wifi", "list", "ifname", dev,
                       "--rescan", "yes", timeout=25)
    nets = {}
    for line in out.splitlines():
        p = nm_split(line)
        if len(p) >= 5 and p[1]:
            n = {"ssid": p[1], "signal": int(p[2] or 0), "security": p[3], "channel": p[4], "active": p[0] == "*"}
            if p[1] not in nets or nets[p[1]]["signal"] < n["signal"]:
                nets[p[1]] = n
    saved = set(saved_wifi())
    for n in nets.values():
        n["saved"] = n["ssid"] in saved or f"wifi-{n['ssid']}" in saved
    res = sorted(nets.values(), key=lambda n: (-n["active"], -n["signal"]))
    STATE["last_scan"] = res
    return res


SEC_LABEL = {"": "open", "none": "WEP", "wpa-psk": "WPA2", "sae": "WPA3", "wpa-eap": "WPA2-Enterprise",
             "owe": "open (versleuteld)"}


def wifi_saved_details():
    """Bewaarde Wi-Fi-netwerken met voorrang, automatisch verbinden, beveiliging, laatst gebruikt en bereik."""
    rc, out, _ = nmcli("-f", "NAME,UUID,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY,TIMESTAMP,ACTIVE", "connection", "show")
    scan = {n["ssid"]: n for n in (STATE.get("last_scan") or [])}
    res = []
    for line in out.splitlines():
        p = nm_split(line)
        if len(p) < 7 or p[2] != "802-11-wireless" or p[0] == AP_CON:
            continue
        props = {}
        _, o2, _ = nmcli("-f", "802-11-wireless.ssid,802-11-wireless.mode,802-11-wireless.hidden,"
                         "802-11-wireless-security.key-mgmt,802-1x.identity", "connection", "show", p[1])
        for l2 in o2.splitlines():
            k, _, v = l2.partition(":")
            props[k] = v
        if props.get("802-11-wireless.mode") == "ap":
            continue
        ssid = props.get("802-11-wireless.ssid") or p[0]
        km = props.get("802-11-wireless-security.key-mgmt", "")
        sc = scan.get(ssid)
        res.append({"name": p[0], "uuid": p[1], "ssid": ssid, "autoconnect": p[3] == "yes",
                    "priority": int(p[4] or 0) if p[4].lstrip("-").isdigit() else 0,
                    "lastUsed": int(p[5]) if p[5].isdigit() and int(p[5]) > 0 else None, "active": p[6] == "yes",
                    "security": SEC_LABEL.get(km, km or "open"), "keyMgmt": km,
                    "hidden": props.get("802-11-wireless.hidden") == "yes",
                    "identity": props.get("802-1x.identity", ""),
                    "signal": sc["signal"] if sc else None})
    res.sort(key=lambda n: (-n["priority"], -(n["lastUsed"] or 0)))
    return res


def _wifi_sec_args(security, password, identity=""):
    if security in ("", "open", None):
        return []
    if security == "wpa-psk":
        if len(password) < 8 and len(password) != 0:
            raise RuntimeError("WPA2-wachtwoord: minstens 8 tekens")
        return ["wifi-sec.key-mgmt", "wpa-psk"] + (["wifi-sec.psk", password] if password else [])
    if security == "sae":
        return ["wifi-sec.key-mgmt", "sae"] + (["wifi-sec.psk", password] if password else [])
    if security == "wpa-eap":                 # bv. eduroam of een bedrijfsnetwerk: PEAP + MSCHAPv2
        if not identity:
            raise RuntimeError("gebruikersnaam nodig voor WPA2-Enterprise")
        a = ["wifi-sec.key-mgmt", "wpa-eap", "802-1x.eap", "peap", "802-1x.phase2-auth", "mschapv2",
             "802-1x.identity", identity]
        return a + (["802-1x.password", password] if password else [])
    raise RuntimeError(f"onbekende beveiliging {security}")


def wifi_add(d):
    """Netwerk bewaren zonder te verbinden (ook als het niet in de buurt is)."""
    ssid = (d.get("ssid") or "").strip()
    if not ssid or len(ssid.encode()) > 32:
        raise RuntimeError("netwerknaam (SSID) ontbreekt of is te lang")
    name = f"wifi-{ssid}"
    sec = d.get("security", "wpa-psk")
    pw = d.get("password", "")
    if sec in ("wpa-psk", "sae") and not pw:
        raise RuntimeError("wachtwoord ontbreekt")
    args = _wifi_sec_args(sec, pw, d.get("identity", ""))
    nmcli("connection", "delete", name)
    rc, out, err = nmcli("connection", "add", "type", "wifi", "ifname", "wlan0", "con-name", name, "ssid", ssid,
                         "connection.autoconnect", "yes" if d.get("autoconnect", True) else "no",
                         "connection.autoconnect-priority", str(int(d.get("priority", 10))),
                         "802-11-wireless.hidden", "yes" if d.get("hidden") else "no",
                         "connection.autoconnect-retries", "0", *args)
    if rc != 0:
        raise RuntimeError((err or out).strip()[:300] or "toevoegen mislukt")
    log(f"Wi-Fi bewaard: {ssid} ({sec})")
    if d.get("connect"):
        nmcli("connection", "up", name, timeout=45)
    return name


def wifi_update(d):
    """Bewaard netwerk aanpassen: voorrang, automatisch verbinden, wachtwoord."""
    name = d.get("name") or ""
    if name not in [n["name"] for n in wifi_saved_details()]:
        raise RuntimeError("onbekend netwerk")
    args = []
    if "autoconnect" in d:
        args += ["connection.autoconnect", "yes" if d["autoconnect"] else "no"]
    if "priority" in d:
        args += ["connection.autoconnect-priority", str(max(-999, min(999, int(d["priority"]))))]
    if d.get("password"):
        km = next(n["keyMgmt"] for n in wifi_saved_details() if n["name"] == name)
        args += ["802-1x.password" if km == "wpa-eap" else "wifi-sec.psk", d["password"]]
    if args:
        nmcli("connection", "modify", name, *args, check=True)
    return True


def wifi_order(names):
    """Volgorde (eerste = hoogste voorrang) omzetten in prioriteiten."""
    known = {n["name"] for n in wifi_saved_details()}
    for i, name in enumerate([n for n in names if n in known]):
        nmcli("connection", "modify", name, "connection.autoconnect-priority", str(100 - i * 5))
    return True


def wifi_up_job(name):
    res = {"ssid": name.replace("wifi-", "", 1), "ok": False, "message": "", "done": False}
    STATE["wifi_job"] = res
    try:
        if ap_active() == "wlan0":
            stop_ap()
            time.sleep(2)
        rc, out, err = nmcli("connection", "up", name, timeout=60)
        res.update(ok=rc == 0, message=f"verbonden met {res['ssid']}" if rc == 0 else ((err or out).strip()[:300] or "verbinden mislukt"))
    finally:
        res["done"] = True
        STATE["wifi_job"] = None
        STATE["last_wifi"] = res
        if not res["ok"] and not uplink():
            start_ap("geen netwerk")


def wifi_connect_job(ssid, password, hidden):
    """Loopt in de achtergrond: tijdens het verbinden kan de hotspot op wlan0 wegvallen."""
    name = f"wifi-{ssid}"
    res = {"ssid": ssid, "ok": False, "message": "", "done": False}
    STATE["wifi_job"] = res
    try:
        ap = ap_active()
        if ap == "wlan0":
            stop_ap()
            time.sleep(2)
        nmcli("connection", "delete", name)
        args = ["device", "wifi", "connect", ssid, "ifname", "wlan0", "name", name]
        if password:
            args += ["password", password]
        if hidden:
            args += ["hidden", "yes"]
        rc, out, err = nmcli(*args, timeout=60)
        if rc == 0:
            nmcli("connection", "modify", name, "connection.autoconnect", "yes",
                  "connection.autoconnect-priority", "10", "connection.autoconnect-retries", "0")
            res.update(ok=True, message=f"verbonden met {ssid}")
        else:
            res["message"] = (err or out).strip()[:300] or "verbinden mislukt"
            nmcli("connection", "delete", name)
    finally:
        res["done"] = True
        STATE["wifi_job"] = None
        STATE["last_wifi"] = res
        if not res["ok"] and not uplink():
            start_ap("geen netwerk")


METRICS = {"ethernet": 100, "wifi": 600, "usb": 700}
DNS_CONF = "/etc/NetworkManager/dnsmasq-shared.d/khzs-dns.conf"
HOSTS_CONF = "/etc/NetworkManager/dnsmasq-shared.d/khzs-hosts.conf"
MAC_RX = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def dhcp_leases():
    """Uitgedeelde adressen op de LAN's van dit kastje (dnsmasq van NetworkManager)."""
    out = []
    for fn in glob.glob("/var/lib/NetworkManager/dnsmasq-*.leases"):
        iface = os.path.basename(fn)[len("dnsmasq-"):-len(".leases")]
        try:
            for line in open(fn):
                f = line.split()
                if len(f) >= 4:
                    out.append({"iface": iface, "expires": int(f[0]), "mac": f[1].lower(), "ip": f[2],
                                "name": "" if f[3] == "*" else f[3]})
        except OSError:
            continue
    return sorted(out, key=lambda x: tuple(int(p) for p in x["ip"].split(".")) if x["ip"].count(".") == 3 else (0,))


def con_of(dev):
    for d in devices():
        if d["device"] == dev and d["connection"] not in ("", "--"):
            return d["connection"]
    return None


def apply_wan(n):
    """Routevoorkeur: de gekozen WAN krijgt de laagste metric; 'auto' = standaard + internetcontrole van NetworkManager.
    Internet uit: geen standaardroute (never-default) – het lokale netwerk blijft gewoon werken."""
    want = n.get("wan", "auto")
    never = "no" if n.get("internet", True) else "yes"
    for d in devices():
        kind = "wifi" if d["type"] == "wifi" else ("ethernet" if d["device"] == "eth0" else "usb") if d["type"] == "ethernet" else None
        con = d["connection"]
        if not kind or con in ("", "--", AP_CON) or (kind == "ethernet" and n["ethernet"]["role"] != "wan"):
            continue
        metric = 50 if want == kind else METRICS[kind]
        nmcli("connection", "modify", con, "ipv4.route-metric", str(metric), "ipv6.route-metric", str(metric),
              "ipv4.never-default", never, "ipv6.never-default", never)
        if d["state"].startswith("connected"):
            nmcli("device", "reapply", d["device"], timeout=20)


def apply_usb(n):
    on = n["usb"]["enabled"] and n.get("internet", True)        # een 4G-modem heeft enkel zin voor internet
    for d in usb_modems():
        nmcli("device", "set", d["device"], "autoconnect", "yes" if on else "no")
        if on:
            if not d["state"].startswith("connected"):
                nmcli("device", "connect", d["device"], timeout=30)
        elif d["state"].startswith("connected"):
            nmcli("device", "disconnect", d["device"])


def apply_reservations(n):
    """Vaste adressen (dhcp-host) voor dnsmasq; geldig voor alle LAN's van dit kastje."""
    lines = [f"dhcp-host={r['mac']},{r['ip']}" + (f",{re.sub(r'[^A-Za-z0-9-]', '', r.get('name') or '')[:40]}" if r.get("name") else "")
             for r in n.get("reservations") or []]
    new = ("# Vaste DHCP-adressen (KHZS, beheer › Netwerk)" + chr(10) + chr(10).join(lines) + chr(10)) if lines else ""
    old = open(HOSTS_CONF).read() if os.path.exists(HOSTS_CONF) else ""
    if new == old:
        return False
    if new:
        with open(HOSTS_CONF, "w") as f:
            f.write(new)
    else:
        os.remove(HOSTS_CONF)
    return True


def apply_lan_dns(n):
    try:
        if apply_reservations(n):
            # dnsmasq van de gedeelde verbindingen opnieuw starten zodat de vaste adressen gelden
            for d in devices():
                if d["state"].startswith("connected") and d["connection"] not in ("", "--"):
                    rc, out, _ = nmcli("-g", "ipv4.method", "connection", "show", d["connection"])
                    if out.strip() == "shared":
                        nmcli("connection", "up", d["connection"], timeout=30)
    except OSError as e:
        log(f"vaste adressen: {e}")
    servers = []
    for lan in n["lan"].values():
        servers += [x.strip() for x in str(lan.get("dns") or "").replace(";", ",").split(",") if x.strip()]
    try:
        if servers:
            with open(DNS_CONF, "w") as f:
                f.write("# DNS-servers voor toestellen op de LAN/hotspot (KHZS)" + chr(10) + "no-resolv" + chr(10)
                        + "".join(f"server={x}" + chr(10) for x in dict.fromkeys(servers)))
        elif os.path.exists(DNS_CONF):
            os.remove(DNS_CONF)
    except OSError as e:
        log(f"dns: {e}")


def apply_network(n=None):
    """Past het volledige routermodel toe (ethernet, Wi-Fi-rol, USB, WAN-voorkeur, DHCP/DNS)."""
    n = n or load_conf()["network"]
    msgs = []
    apply_lan_dns(n)
    set_ethernet(n)
    msgs.append("ethernet: " + {"wan": "WAN", "lan": "LAN (DHCP + NAT)", "off": "uit"}[n["ethernet"]["role"]])
    apply_usb(n)
    role = n["wifi"]["role"]
    ap = ap_active()
    if role == "off":
        if ap:
            stop_ap()
        nmcli("device", "disconnect", "wlan0")
    elif role == "client":
        if ap and STATE.get("ap_reason") != "geen netwerk":
            stop_ap()
        nmcli("device", "set", "wlan0", "autoconnect", "yes")
    else:
        if ap:
            stop_ap()
        start_ap("altijd aan" if role == "client+hotspot" else "hotspot-modus")
    msgs.append("Wi-Fi: " + role)
    apply_wan(n)
    msgs.append("WAN: " + n.get("wan", "auto"))
    return msgs


CONFIRM_S = 120


def network_change(new_net, c):
    """Past een netwerkwijziging toe met vangnet: wordt ze niet binnen 2 minuten bevestigd vanuit het beheer
    (bv. omdat je de Pi niet meer bereikt), dan komt de vorige instelling automatisch terug."""
    old = json.loads(json.dumps(c["network"]))
    c["network"] = new_net
    validate_network(new_net)
    save_conf(c)
    msgs = apply_network(new_net)
    STATE["confirm"] = {"deadline": time.time() + CONFIRM_S, "previous": old, "applied": msgs}
    return msgs


def network_confirm():
    STATE["confirm"] = None


def network_revert(reason="niet bevestigd"):
    cf = STATE.get("confirm")
    if not cf:
        return
    STATE["confirm"] = None
    log(f"netwerkwijziging teruggedraaid ({reason})")
    c = load_conf()
    c["network"] = cf["previous"]
    save_conf(c)
    try:
        apply_network(c["network"])
    except Exception as e:
        log(f"terugdraaien: {e}")
    STATE["last_revert"] = {"at": time.time(), "reason": reason}


def reset_network():
    """Herstel naar de fabrieksinstellingen van het netwerk (ook via 'herstel = netwerk' in het SD-bestand)."""
    c = load_conf()
    c["network"] = json.loads(json.dumps(DEFAULTS["network"]))
    c["hotspot"]["ssid"], c["hotspot"]["password"] = DEFAULTS["hotspot"]["ssid"], DEFAULTS["hotspot"]["password"]
    save_conf(c)
    apply_network(c["network"])
    log("netwerk hersteld naar de standaardinstellingen")


def validate_network(n):
    import ipaddress
    if n.get("wan") not in ("auto", "ethernet", "wifi", "usb"):
        raise RuntimeError("ongeldige WAN-keuze")
    if n["ethernet"].get("role") not in ("wan", "lan", "off"):
        raise RuntimeError("ongeldige rol voor ethernet")
    if n["wifi"].get("role") not in ("client", "hotspot", "client+hotspot", "off"):
        raise RuntimeError("ongeldige rol voor Wi-Fi")
    # de noodhotspot blijft altijd beschikbaar, dus 'alles uit' kan de Pi niet onbereikbaar maken
    if n["ethernet"]["role"] == "wan" and n["ethernet"].get("ipv4") == "static":
        try:
            ipaddress.ip_interface(n["ethernet"].get("address", ""))
        except ValueError:
            raise RuntimeError("vast IP-adres ongeldig (bv. 192.168.1.50/24)")
    nets = []
    nets = []
    for lan in n["lan"].values():
        try:
            nets.append(ipaddress.ip_interface(lan["address"]).network)
        except (ValueError, KeyError):
            pass
    seen = set()
    for r in n.get("reservations") or []:
        mac = str(r.get("mac") or "").strip().lower().replace("-", ":")
        if not MAC_RX.match(mac):
            raise RuntimeError(f"vast adres: ongeldig MAC-adres {r.get('mac')}")
        try:
            ip = ipaddress.ip_address(str(r.get("ip") or "").strip())
        except ValueError:
            raise RuntimeError(f"vast adres: ongeldig IP-adres {r.get('ip')}")
        if not any(ip in net for net in nets):
            raise RuntimeError(f"vast adres {ip} ligt niet in een LAN van dit kastje")
        if mac in seen or str(ip) in seen:
            raise RuntimeError(f"vast adres: {mac} of {ip} komt twee keer voor")
        seen |= {mac, str(ip)}
        r["mac"], r["ip"] = mac, str(ip)
    for name, lan in n["lan"].items():
        try:
            itf = ipaddress.ip_interface(lan["address"])
            a, b = ipaddress.ip_address(lan["dhcp_start"]), ipaddress.ip_address(lan["dhcp_end"])
        except (ValueError, KeyError):
            raise RuntimeError(f"{name}: ongeldig adres of DHCP-bereik")
        if a not in itf.network or b not in itf.network or a > b or itf.ip in (a, b):
            raise RuntimeError(f"{name}: DHCP-bereik moet binnen {itf.network} liggen en het adres van de Pi niet bevatten")
        if not 0.1 <= float(lan.get("lease_h") or 1) <= 168:
            raise RuntimeError(f"{name}: leasetijd tussen 0,1 en 168 uur")
        nets.append(itf.network)
    if len(nets) == 2 and nets[0].overlaps(nets[1]):
        raise RuntimeError("het ethernet-LAN en de hotspot moeten een ander adresbereik hebben")


def set_ethernet(cfg):
    if "role" in cfg:                                   # nieuw routermodel → vertalen naar mode
        n = cfg
        e = n["ethernet"]
        cfg = {"mode": {"lan": "share", "off": "off"}.get(e["role"], "static" if e.get("ipv4") == "static" else "dhcp"),
               "address": n["lan"]["ethernet"]["address"] if e["role"] == "lan" else e.get("address", ""),
               "gateway": e.get("gateway", ""), "dns": e.get("dns", ""), "_lan": n["lan"]["ethernet"]}
    mode = cfg.get("mode", "dhcp")
    nmcli("connection", "delete", ETH_CON)
    for d in devices():                       # standaardprofielen op eth0 opruimen
        if d["device"] == "eth0" and d["connection"] not in ("", "--", ETH_CON):
            nmcli("connection", "delete", d["connection"])
    args = ["connection", "add", "type", "ethernet", "ifname", "eth0", "con-name", ETH_CON,
            "autoconnect", "yes", "ipv6.method", "auto"]
    if mode == "static":
        if not cfg.get("address"):
            raise RuntimeError("vast IP-adres ontbreekt (bv. 192.168.1.50/24)")
        args += ["ipv4.method", "manual", "ipv4.addresses", cfg["address"]]
        if cfg.get("gateway"):
            args += ["ipv4.gateway", cfg["gateway"]]
        if cfg.get("dns"):
            args += ["ipv4.dns", cfg["dns"].replace(" ", ",")]
    elif mode == "share":
        args += ["ipv4.method", "shared", "ipv6.method", "disabled"] + \
            shared_args(cfg.get("_lan") or {"address": cfg.get("address") or "10.43.0.1/24"})
    elif mode == "off":
        args += ["ipv4.method", "disabled", "ipv6.method", "disabled", "connection.autoconnect", "no"]
    else:
        args += ["ipv4.method", "auto"]
    nmcli(*args, check=True)
    nmcli("connection", "up", ETH_CON, timeout=30)


# ---------------------------------------------------------------- kiosk (HDMI)
def apply_kiosk(k):
    with open(KIOSK_ENV, "w") as f:
        f.write(f"KIOSK_URL={k.get('url') or 'http://localhost/jury'}\nKIOSK_ZOOM={float(k.get('zoom') or 1.0)}\n"
                f"KIOSK_MODE={k.get('mode') or 'auto'}\nKIOSK_ROTATE={int(k.get('rotate') or 0)}\n")
    if k.get("enabled", True):
        run(["systemctl", "enable", "khzs-kiosk.service"])
        run(["systemctl", "restart", "khzs-kiosk.service"])
    else:
        run(["systemctl", "disable", "--now", "khzs-kiosk.service"])


# ---------------------------------------------------------------- hotspot-login (browser op afstand via CDP)
class Portal:
    PORT = 9223

    def __init__(self):
        self.proc = None
        self.last = 0.0
        self.lock = threading.Lock()
        self.msgid = 0

    def start(self, url="http://neverssl.com/"):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            prof = os.path.join(DATA, "portal-profile")
            os.makedirs(prof, exist_ok=True)
            shutil.chown(prof, "kiosk", "kiosk")
            self.proc = subprocess.Popen(
                ["runuser", "-u", "kiosk", "--", "chromium", "--headless=new", "--disable-gpu",
                 f"--remote-debugging-port={self.PORT}", "--remote-debugging-address=127.0.0.1",
                 f"--user-data-dir={prof}", "--window-size=1024,768", "--no-first-run",
                 # gewone Chrome-identiteit: sommige portals weigeren "HeadlessChrome"
                 "--user-agent=Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                 "--disable-features=Translate", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.last = time.time()
            for _ in range(40):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{self.PORT}/json", timeout=1).read()
                    return
                except OSError:
                    time.sleep(0.5)
            raise RuntimeError("browser start niet")

    def stop(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None

    def idle_reaper(self):
        while True:
            time.sleep(30)
            if self.proc and time.time() - self.last > 600:
                self.stop()

    def _ws(self):
        pages = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.PORT}/json", timeout=3).read())
        page = next(p for p in pages if p.get("type") == "page")
        return MiniWS(page["webSocketDebuggerUrl"])

    def call(self, *cmds):
        """Voert CDP-commando's uit (method, params) en geeft het laatste resultaat terug."""
        self.last = time.time()
        ws = self._ws()
        try:
            res = None
            for method, params in cmds:
                self.msgid += 1
                mid = self.msgid
                ws.send(json.dumps({"id": mid, "method": method, "params": params}))
                t0 = time.time()
                while time.time() - t0 < 20:
                    m = json.loads(ws.recv())
                    if m.get("id") == mid:
                        if "error" in m:
                            raise RuntimeError(m["error"].get("message", "CDP-fout"))
                        res = m.get("result", {})
                        break
            return res
        finally:
            ws.close()

    def screenshot(self):
        r = self.call(("Page.captureScreenshot", {"format": "png"}))
        info = self.call(("Runtime.evaluate", {"expression": "JSON.stringify([location.href, document.title])",
                                              "returnByValue": True}))
        try:
            href, title = json.loads(info["result"]["value"])
        except (KeyError, ValueError, TypeError):
            href, title = "", ""
        return base64.b64decode(r["data"]), href, title

    def click(self, x, y):
        base = {"x": x, "y": y, "button": "left", "clickCount": 1}
        self.call(("Input.dispatchMouseEvent", dict(base, type="mousePressed")),
                  ("Input.dispatchMouseEvent", dict(base, type="mouseReleased")))

    def type(self, text):
        self.call(("Input.insertText", {"text": text}))

    def key(self, key):
        codes = {"Enter": 13, "Tab": 9, "Backspace": 8, "Escape": 27}
        k = {"key": key, "code": key, "windowsVirtualKeyCode": codes.get(key, 0)}
        self.call(("Input.dispatchKeyEvent", dict(k, type="keyDown")), ("Input.dispatchKeyEvent", dict(k, type="keyUp")))

    def navigate(self, url):
        self.call(("Page.navigate", {"url": url}))


class MiniWS:
    """Minimale websocket-client (enkel voor localhost CDP)."""

    def __init__(self, url):
        u = urllib.parse.urlsplit(url)
        self.s = socket.create_connection((u.hostname, u.port), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = self.s.recv(4096)
            if not c:
                raise RuntimeError("websocket handshake")
            buf += c
        head, _, self.rbuf = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("websocket geweigerd")

    def send(self, text):
        p = text.encode()
        hdr = bytearray([0x81])
        n = len(p)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += n.to_bytes(2, "big")
        else:
            hdr.append(0x80 | 127)
            hdr += n.to_bytes(8, "big")
        mask = os.urandom(4)
        hdr += mask
        self.s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(p)))

    def _read(self, n):
        while len(self.rbuf) < n:
            c = self.s.recv(65536)
            if not c:
                raise RuntimeError("websocket gesloten")
            self.rbuf += c
        d, self.rbuf = self.rbuf[:n], self.rbuf[n:]
        return d

    def recv(self):
        data = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = int.from_bytes(self._read(2), "big")
            elif n == 127:
                n = int.from_bytes(self._read(8), "big")
            data += self._read(n)
            if b0 & 0x80:
                return data.decode("utf-8", "replace")

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


PORTAL = Portal()


# ---------------------------------------------------------------- updates
def ver_tuple(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-+]", str(v or "0")))


def ulog(msg):
    with LOCK:
        STATE["update"]["log"].append(time.strftime("%H:%M:%S ") + msg)
        del STATE["update"]["log"][:-60]
    log("update: " + msg)


def timing_idle(minutes):
    """Geen update tijdens een wedstrijd: klok staat stil en al 'minutes' minuten geen gegevens van het tijdsysteem."""
    try:
        d = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}/state", timeout=5).read())
    except (OSError, ValueError):
        return True                        # server antwoordt niet: updaten mag (kan net de oplossing zijn)
    if (d.get("clock") or {}).get("status") == "running":
        return False
    ago = d.get("lastPacketAgo")
    return ago is None or ago > minutes * 60


def install_release(blob, source="upload"):
    with LOCK:
        if STATE["update"]["busy"]:
            raise RuntimeError("er loopt al een update")
        STATE["update"]["busy"] = True
        STATE["update"]["log"] = []
    tmp = None
    try:
        ulog(f"pakket ontvangen ({len(blob) // 1024} KB, {source})")
        zf = zipfile.ZipFile(io.BytesIO(blob))
        meta = json.loads(zf.read("release.json"))
        ver = str(meta["version"])
        if not re.fullmatch(r"[0-9A-Za-z.\-+]{1,40}", ver):
            raise RuntimeError("ongeldig versienummer")
        ulog(f"versie {ver} (nu {app_version() or '?'})")
        for name, digest in meta["files"].items():
            if hashlib.sha256(zf.read("app/" + name)).hexdigest() != digest:
                raise RuntimeError(f"controlesom klopt niet: {name}")
        ulog(f"{len(meta['files'])} bestanden gecontroleerd")
        os.makedirs(RELEASES, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=f".{ver}-", dir=RELEASES)
        for name in meta["files"]:
            dst = os.path.normpath(os.path.join(tmp, name))
            if not dst.startswith(tmp + os.sep):
                raise RuntimeError(f"ongeldig pad in pakket: {name}")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(zf.read("app/" + name))
        run(["chown", "-R", "root:root", tmp])
        run(["chmod", "-R", "a+rX,go-w", tmp])
        rc, out, err = run(["runuser", "-u", "khzs", "--", "env", f"KHZS_DATA={DATA}", "python3",
                            os.path.join(tmp, "livetiming.py"), "--selftest"], timeout=90)
        if rc != 0 or "selftest OK" not in out:
            raise RuntimeError("zelftest mislukt: " + (err or out).strip()[-300:])
        ulog("zelftest geslaagd")
        final = os.path.join(RELEASES, ver + "-" + time.strftime("%Y%m%d%H%M%S"))
        os.rename(tmp, final)
        tmp = None
        prev = os.path.realpath(CURRENT) if os.path.exists(CURRENT) else None
        switch_current(final)
        ulog("nieuwe versie actief, server herstart")
        run(["systemctl", "restart", "khzs-timing.service"])
        if wait_version(ver, 45):
            ulog(f"versie {ver} draait")
            STATE["update"]["last"] = {"ok": True, "version": ver, "at": time.time(), "source": source}
            cleanup_releases(keep=[final, prev])
            try:
                with open(FAILS, "w") as f:
                    f.write("0")
            except OSError:
                pass
            pend = system_check()
            if pend:
                ulog(f"{len(pend)} systeemwijziging(en) wachten op bevestiging in het beheer")
            ulog("systeemdienst herstart met de nieuwe versie")
            threading.Timer(3, lambda: run(["systemctl", "restart", "khzs-agent.service"])).start()
            return {"ok": True, "version": ver, "systemPending": pend}
        ulog("nieuwe versie antwoordt niet: terug naar de vorige")
        if prev:
            switch_current(prev)
            run(["systemctl", "restart", "khzs-timing.service"])
        STATE["update"]["last"] = {"ok": False, "version": ver, "at": time.time(), "source": source,
                                   "message": "teruggedraaid"}
        raise RuntimeError("nieuwe versie startte niet; vorige versie teruggezet")
    except Exception as e:
        ulog(f"FOUT: {e}")
        if STATE["update"].get("last") is None or STATE["update"]["last"].get("ok"):
            STATE["update"]["last"] = {"ok": False, "at": time.time(), "source": source, "message": str(e)}
        raise
    finally:
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        STATE["update"]["busy"] = False


def switch_current(target):
    tmp_link = CURRENT + ".new"
    if os.path.lexists(tmp_link):
        os.remove(tmp_link)
    os.symlink(target, tmp_link)
    os.replace(tmp_link, CURRENT)


def wait_version(ver, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            d = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}/api/version", timeout=3).read())
            if d.get("version") == ver:
                return True
        except (OSError, ValueError):
            pass
        time.sleep(2)
    return False


def cleanup_releases(keep):
    keep = {os.path.realpath(k) for k in keep if k}
    dirs = sorted((d for d in glob.glob(os.path.join(RELEASES, "*")) if os.path.isdir(d) and not
                   os.path.basename(d).startswith(".")), key=os.path.getmtime, reverse=True)
    for d in dirs[3:]:
        if os.path.realpath(d) not in keep:
            shutil.rmtree(d, ignore_errors=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def gh_request(url, token, accept="application/vnd.github+json"):
    h = {"Accept": accept, "User-Agent": "khzs-timing-updater", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    op = urllib.request.build_opener(NoRedirect)
    try:
        r = op.open(urllib.request.Request(url, headers=h), timeout=30)
        return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):         # download-URL (zonder token volgen)
            loc = e.headers.get("Location")
            return urllib.request.urlopen(urllib.request.Request(loc, headers={"User-Agent": "khzs-timing-updater"}),
                                          timeout=120).read()
        raise RuntimeError(f"GitHub antwoordt {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")


def gh_check():
    u = load_conf()["update"]
    if not u.get("repo"):
        raise RuntimeError("geen GitHub-repo ingesteld")
    rel = json.loads(gh_request(f"https://api.github.com/repos/{u['repo']}/releases/latest", u.get("token")))
    asset = next((a for a in rel.get("assets", []) if re.match(r"khzs-timing-.*\.zip$", a.get("name", ""))), None)
    ver = (rel.get("tag_name") or "").lstrip("vV")
    info = {"version": ver, "name": rel.get("name") or rel.get("tag_name"), "notes": (rel.get("body") or "")[:2000],
            "published": rel.get("published_at"), "asset": asset and asset["url"],
            "newer": ver_tuple(ver) > ver_tuple(app_version()), "checked": time.time()}
    STATE["update"]["available"] = info
    return info


def gh_apply():
    info = gh_check()
    if not info.get("asset"):
        raise RuntimeError("de laatste release bevat geen khzs-timing-*.zip")
    blob = gh_request(info["asset"], load_conf()["update"].get("token"), accept="application/octet-stream")
    return install_release(blob, source=f"GitHub {info['version']}")


def cloud_request(path, binary=False):
    st = app_settings()
    url, tok = (st.get("relay_url") or "").rstrip("/"), st.get("relay_token") or ""
    if not url or not tok:
        raise RuntimeError("geen publieke server ingesteld (Doorsturen)")
    req = urllib.request.Request(url + path, headers={"X-Ingest-Token": tok, "User-Agent": "khzs-agent"})
    data = urllib.request.urlopen(req, timeout=120).read()
    return data if binary else json.loads(data)


def cloud_check():
    rel = cloud_request("/tunnel/release").get("release")
    if not rel:
        raise RuntimeError("de publieke server heeft nog geen pakket (stuur er een vanuit een lokaal beheer › Cloudserver)")
    info = dict(rel, newer=ver_tuple(rel.get("version") or "0") > ver_tuple(app_version()), checked=time.time())
    STATE["update"]["cloud"] = info
    return info


def cloud_apply():
    info = cloud_check()
    blob = cloud_request("/tunnel/release.zip", binary=True)
    if hashlib.sha256(blob).hexdigest() != info.get("sha256"):
        raise RuntimeError("download onvolledig (controlesom klopt niet)")
    return install_release(blob, source=f"publieke server {info.get('version')}")


def auto_updater():
    time.sleep(600)
    while True:
        try:
            u = load_conf()["update"]
            if u.get("auto") and STATE["internet"]:
                info = gh_check()
                if info.get("newer"):
                    if timing_idle(int(u.get("idle_min", 15))):
                        log(f"automatische update naar {info['version']}")
                        gh_apply()
                    else:
                        log("update beschikbaar, maar de wedstrijd loopt: later opnieuw")
        except Exception as e:
            log(f"auto-update: {e}")
        time.sleep(3 * 3600)


# ---------------------------------------------------------------- systeemwijzigingen (enkel na bevestiging)
def system_check():
    st = STATE["system"]
    if not os.path.exists(APPLY):
        st.update(pending=[], checked=time.time())
        return []
    rc, out, err = run(["bash", APPLY, "--dry-run"], timeout=60)
    pend = [l[len("WIJZIGING: "):] for l in out.splitlines() if l.startswith("WIJZIGING: ")]
    st.update(pending=pend, checked=time.time())
    return pend


def system_apply():
    st = STATE["system"]
    if st["busy"]:
        raise RuntimeError("systeemwijzigingen worden al toegepast")
    st.update(busy=True, log=[])
    try:
        rc, out, err = run(["bash", APPLY], timeout=1800)
        st["log"] = (out + err).strip().splitlines()[-80:]
        if rc != 0 or any(l.startswith("FOUT") for l in st["log"]):
            raise RuntimeError("niet alles lukte – zie het logboek")
        return {"ok": True, "log": st["log"]}
    finally:
        st["busy"] = False
        system_check()


# ---------------------------------------------------------------- rol (server / scherm) en naam
def apply_role(c):
    d = c["device"]
    role = d.get("role", "uit")
    with open(ROLE_ENV, "w") as f:
        f.write("KHZS_ROLE=" + {"server": "server", "scherm": "display"}.get(role, "off") + chr(10))
    run(["chown", "khzs:khzs", ROLE_ENV])
    k = dict(c["kiosk"])
    if role != "server":
        k["url"] = "http://localhost/display"        # scherm: bron tonen; uit: statusscherm met naam en adressen
    apply_kiosk(k)
    run(["systemctl", "restart", "khzs-timing.service"])


def reachable(url):
    try:
        u = urllib.parse.urlsplit(url)
        socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=5).close()
        return True
    except (OSError, ValueError, TypeError):
        return False


def kiosk_page():
    """Huidige pagina van het HDMI-scherm (Chromium remote-debugging, enkel localhost)."""
    try:
        pages = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=3).read())
        return next((p for p in pages if p.get("type") == "page"), None)
    except (OSError, ValueError):
        return None


def kiosk_navigate(url):
    p = kiosk_page()
    if not p:
        return False
    try:
        ws = MiniWS(p["webSocketDebuggerUrl"])
        ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": url}}))
        ws.close()
        return True
    except Exception as e:
        log(f"scherm: navigeren mislukt: {e}")
        return False


def local_server_url():
    """Een server op hetzelfde netwerk (gevonden via de aankondigingen van livetiming), voorkeur: met live gegevens."""
    try:
        peers = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}/api/peers", timeout=3).read()).get("peers", [])
    except (OSError, ValueError):
        return None
    servers = sorted((p for p in peers if p.get("role") == "server"), key=lambda p: (not p.get("live"), p.get("ago", 99)))
    return servers[0]["url"] if servers else None


def display_source(d):
    """Bron van het schermkastje; "auto" of "auto:/pad" = de server op hetzelfde netwerk (standaard de oproepkamer)."""
    u = d.get("display_url") or ""
    if u == "auto" or u.startswith("auto:"):
        srv = local_server_url()
        return (srv + (u[5:] or "/callroom")) if srv else ""
    return u


def display_backup(d):
    b = d.get("display_backup_url") or ""
    if b == "auto":
        srv = local_server_url()
        if not srv:
            return ""
        u = d.get("display_url") or ""
        path = (u[5:] or "/callroom") if u.startswith("auto") else (urllib.parse.urlsplit(u).path or "/")
        return srv + path
    return b


KIOSK_LABEL_JS = """(function(){if(window.top!==window)return;var L=%s;function add(){var d=document.getElementById('__khzs_lbl');
if(!d){d=document.createElement('div');d.id='__khzs_lbl';d.style.cssText='position:fixed;right:6px;bottom:4px;z-index:2147483647;font:600 11px system-ui,sans-serif;color:rgba(255,255,255,.6);background:rgba(0,0,0,.3);padding:1px 7px;border-radius:6px;pointer-events:none;letter-spacing:.02em';(document.body||document.documentElement).appendChild(d)}d.textContent=L}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',add);else add()})();"""


def kiosk_label():
    """Naam van dit kastje klein rechtsonder op ELKE pagina van het HDMI-scherm (ook pagina's van de cloudserver).
    Enkel in de Chromium van dit scherm; bezoekers van de site zien het nooit. Houdt een CDP-sessie open (localhost)."""
    while True:
        ws = None
        try:
            pg = kiosk_page()
            if not pg:
                time.sleep(10)
                continue
            ws = MiniWS(pg["webSocketDebuggerUrl"])
            ws.s.settimeout(30)
            cur, script_id, n = None, None, 10
            while True:
                label = app_settings().get("device_label") or socket.gethostname()
                if label != cur:
                    js = KIOSK_LABEL_JS % json.dumps(label)
                    if script_id:
                        n += 1
                        ws.send(json.dumps({"id": n, "method": "Page.removeScriptToEvaluateOnNewDocument", "params": {"identifier": script_id}}))
                    n += 1
                    ws.send(json.dumps({"id": n, "method": "Page.addScriptToEvaluateOnNewDocument", "params": {"source": js}}))
                    want = n
                    n += 1
                    ws.send(json.dumps({"id": n, "method": "Runtime.evaluate", "params": {"expression": js}}))
                    cur = label
                    script_id = None
                    t0 = time.time()
                    while time.time() - t0 < 10:
                        m = json.loads(ws.recv())
                        if m.get("id") == want:
                            script_id = (m.get("result") or {}).get("identifier")
                            break
                # sessie levend houden (anders vervalt het script); elke 20 s een ping, label om de 20 s nakijken
                n += 1
                ws.send(json.dumps({"id": n, "method": "Runtime.evaluate", "params": {"expression": "1"}}))
                t0 = time.time()
                while time.time() - t0 < 20:
                    try:
                        ws.recv()
                    except socket.timeout:
                        break
        except Exception:
            time.sleep(5)                     # scherm herstart of nog niet klaar: later opnieuw
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass


def display_watch():
    """HDMI-scherm zelfherstellend: foutpagina van Chromium -> opnieuw laden; rol scherm: bron weg -> reservebron
    (als ingesteld), bron terug -> terug naar de bron. Chromium blijft anders op een foutpagina staan."""
    was_ok, on_backup, err_since = True, False, None
    while True:
        time.sleep(15)
        try:
            d = load_conf()["device"]
            c = load_conf()["kiosk"]
            if not c.get("enabled", True):
                continue
            pg = kiosk_page()
            cur = (pg or {}).get("url", "")
            if cur.startswith("chrome-error://"):
                err_since = err_since or time.time()
                if time.time() - err_since > 20:              # foutpagina blijft staan: opnieuw naar de startpagina
                    log("scherm toont een foutpagina: opnieuw laden")
                    kiosk_navigate("http://localhost/display" if d.get("role") == "scherm" else (c.get("url") or "http://localhost/jury"))
                    err_since = None
            else:
                err_since = None
            if d.get("role") != "scherm" or not d.get("display_url"):
                was_ok, on_backup = True, False
                continue
            ok = reachable(display_source(d))
            backup = display_backup(d)
            if not ok and backup and not on_backup and not was_ok and reachable(backup):
                log("bron van het scherm onbereikbaar: reservebron " + backup)
                if kiosk_navigate("http://localhost/display?bron=reserve"):
                    on_backup = True
            elif ok and (on_backup or not was_ok):
                log("bron van het scherm terug bereikbaar: terug naar de bron")
                if not kiosk_navigate("http://localhost/display"):
                    run(["systemctl", "restart", "khzs-kiosk.service"])
                on_backup = False
            was_ok = ok
        except Exception as e:
            log(f"display_watch: {e}")


def set_hostname(name):
    name = (name or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?", name):
        raise RuntimeError("naam: kleine letters, cijfers en '-' (max. 32), bv. khzs-scherm-inkom")
    if socket.gethostname() == name:
        return
    run(["hostnamectl", "set-hostname", name], check=True)
    try:
        lines = [l for l in open("/etc/hosts").read().splitlines() if not l.startswith("127.0.1.1")]
        lines.append(f"127.0.1.1\t{name}")
        with open("/etc/hosts", "w") as f:
            f.write(chr(10).join(lines) + chr(10))
    except OSError as e:
        log(f"hosts: {e}")
    run(["systemctl", "restart", "avahi-daemon"])


# ---------------------------------------------------------------- instellingenbestand op de SD-kaart (FAT, leesbaar in Windows)
SD_KEYS = [
    ("naam", "naam van dit kastje (ook adres: <naam>.local)"),
    ("rol", "uit | server | scherm   (uit = enkel beheer; server = live timing; scherm = toont een andere bron)"),
    ("herstel", "zet op 'netwerk' om alle netwerkinstellingen terug te zetten naar de standaard (eenmalig)"),
    ("internet", "aan | uit  (verbinding met internet toestaan)"),
    ("scherm_pagina", "rol server: pagina op het HDMI-scherm, bv. http://localhost/jury"),
    ("scherm_bron", "rol scherm: wat het scherm toont, bv. https://timing.khzs.be/callroom of http://khzs-server.local/jury"),
    ("scherm_sleutel", "rol scherm: wachtwoord voor /jury op de publieke server (leeg = ongewijzigd)"),
    ("scherm_zoom", "vergroting van het scherm, bv. 1.0 of 1.5"),
    ("scherm_reserve", "rol scherm: reservebron als de bron wegvalt: auto (= server op hetzelfde netwerk), een adres, of leeg"),
    ("wifi_rol", "client | hotspot | client+hotspot | uit"),
    ("wifi_netwerk", "Wi-Fi-netwerk om mee te verbinden"),
    ("wifi_wachtwoord", "wachtwoord van dat netwerk (leeg = ongewijzigd)"),
    ("wifi_toevoegen", "extra netwerk bewaren zonder te verbinden: NAAM | WACHTWOORD  (meerdere: scheiden met ;;)"),
    ("hotspot_naam", "naam van de eigen hotspot"),
    ("hotspot_wachtwoord", "wachtwoord eigen hotspot, min. 8 tekens (leeg = ongewijzigd)"),
    ("noodhotspot", "altijd aan als vangnet (kan niet uit)"),
    ("noodhotspot_na_s", "seconden zonder netwerk voor de noodhotspot start"),
    ("ethernet", "wan | lan | uit"),
    ("ethernet_ip", "dhcp of een vast adres zoals 192.168.1.50/24"),
    ("ethernet_gateway", "gateway bij een vast adres"),
    ("ethernet_dns", "DNS bij een vast adres, bv. 1.1.1.1"),
    ("usb_modem", "aan | uit  (4G-router of gsm via USB)"),
    ("wan_voorkeur", "auto | ethernet | wifi | usb"),
    ("beheerderscode", "code voor het beheer (leeg = ongewijzigd)"),
    ("ssh", "aan | uit"),
    ("ssh_wachtwoord_login", "aan | uit"),
    ("ssh_sleutel", "publieke SSH-sleutel voor gebruiker khzs (wordt toegevoegd)"),
    ("updates_automatisch", "aan | uit  (van GitHub, nooit tijdens een reeks)"),
    ("publieke_server", "adres van de publieke server, bv. https://timing.khzs.be"),
    ("publieke_server_token", "token van de publieke server (leeg = ongewijzigd)"),
    ("beheer_op_afstand", "aan | uit  (dit kastje beheren via de publieke server: …/kastje/<naam>/settings)"),
]
SECRETS = {"publieke_server_token", "wifi_toevoegen", "wifi_wachtwoord", "hotspot_wachtwoord", "beheerderscode", "scherm_sleutel", "ssh_sleutel"}


def sd_values(c):
    n = c["network"]
    e = n["ethernet"]
    cur = active_wifi() if os.path.exists("/sys/class/net/wlan0") else None
    sshs = ssh_status()
    return {
        "naam": socket.gethostname(), "rol": c["device"]["role"], "scherm_pagina": c["kiosk"]["url"],
        "scherm_bron": c["device"]["display_url"], "scherm_reserve": c["device"].get("display_backup_url", ""), "scherm_zoom": str(c["kiosk"]["zoom"]),
        "wifi_rol": {"off": "uit"}.get(n["wifi"]["role"], n["wifi"]["role"]),
        "wifi_netwerk": (cur or {}).get("ssid", ""), "hotspot_naam": c["hotspot"]["ssid"],
        "noodhotspot": "aan", "noodhotspot_na_s": str(c["hotspot"]["after_s"]),
        "ethernet": {"off": "uit"}.get(e["role"], e["role"]),
        "ethernet_ip": e.get("address") if e.get("ipv4") == "static" else "dhcp",
        "ethernet_gateway": e.get("gateway", ""), "ethernet_dns": e.get("dns", ""),
        "usb_modem": "aan" if n["usb"]["enabled"] else "uit", "wan_voorkeur": n.get("wan", "auto"),
        "internet": "aan" if n.get("internet", True) else "uit", "herstel": "",
        "ssh": "aan" if sshs["enabled"] else "uit", "ssh_wachtwoord_login": "aan" if sshs["passwordAuth"] else "uit",
        "updates_automatisch": "aan" if c["update"].get("auto") else "uit",
        "publieke_server": app_settings().get("relay_url", ""),
        "beheer_op_afstand": "aan" if app_settings().get("remote_admin") else "uit",
    }


def app_settings():
    """Instellingen van de live-timingserver (enkel lezen; schrijven gaat via de server zelf)."""
    try:
        with open(os.path.join(DATA, "settings.json"), encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_sd_file(c=None):
    if not os.path.isdir(os.path.dirname(BOOT_FILE)):
        return
    c = c or load_conf()
    v = sd_values(c)
    nl = chr(13) + chr(10)                       # Windows-regeleinden: leesbaar in Kladblok
    out = ["# KHZS Timing – instellingen van dit kastje", "# Wijzig wat je wil, sla op, steek de kaart terug in de Pi en start hem.",
           "# Wachtwoorden en sleutels worden na het toepassen weer leeggemaakt (leeg = ongewijzigd).",
           f"# Bijgewerkt door de Pi op {time.strftime('%d/%m/%Y %H:%M')}", ""]
    for err in STATE.get("sd_errors") or []:
        out.append(f"# FOUT bij vorige keer: {err}")
    if STATE.get("sd_errors"):
        out.append("")
    for k, help_ in SD_KEYS:
        out.append(f"# {help_}")
        out.append(f"{k} = {'' if k in SECRETS else v.get(k, '')}")
        out.append("")
    data = nl.join(out).encode("utf-8")
    tmp = BOOT_FILE + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, BOOT_FILE)
    with open(SD_HASH, "w") as f:
        f.write(hashlib.sha256(data).hexdigest())


def read_sd_file():
    """Bij het opstarten: als het bestand op de kaart gewijzigd werd (bv. in Windows), de wijzigingen toepassen."""
    if not os.path.exists(BOOT_FILE):
        write_sd_file()
        return
    raw = open(BOOT_FILE, "rb").read()
    try:
        if open(SD_HASH).read().strip() == hashlib.sha256(raw).hexdigest():
            return                                   # niets gewijzigd sinds de Pi het schreef
    except OSError:
        pass
    vals = {}
    for line in raw.decode("utf-8-sig", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip().lower()] = v.strip()
    log(f"instellingenbestand op de SD-kaart gewijzigd: {len(vals)} regels")
    errs = apply_sd_values(vals)
    STATE["sd_errors"] = errs
    write_sd_file()


def apply_sd_values(vals):
    c = load_conf()
    cur = sd_values(c)
    errs = []
    yes = {"aan": True, "ja": True, "on": True, "1": True, "uit": False, "nee": False, "off": False, "0": False}

    def changed(k):
        return k in vals and vals[k] != "" and vals[k] != cur.get(k, "")

    def step(name, fn):
        try:
            fn()
        except Exception as e:
            errs.append(f"{name}: {e}")
    n = c["network"]
    if vals.get("herstel", "").lower() == "netwerk":
        try:
            reset_network()
            c = load_conf()
            n = c["network"]
        except Exception as e:
            errs.append(f"herstel: {e}")
    if changed("internet"):
        n["internet"] = yes.get(vals["internet"].lower(), True)
    if changed("naam"):
        step("naam", lambda: set_hostname(vals["naam"]))
    if changed("rol"):
        if vals["rol"] in ("uit", "server", "scherm"):
            c["device"]["role"] = vals["rol"]
        else:
            errs.append("rol: uit, server of scherm")
    for k, key in (("scherm_bron", "display_url"), ("scherm_reserve", "display_backup_url")):
        if changed(k):
            c["device"][key] = vals[k]
    if vals.get("scherm_sleutel"):
        c["device"]["display_key"] = vals["scherm_sleutel"]
    if changed("scherm_pagina"):
        c["kiosk"]["url"] = vals["scherm_pagina"]
    if changed("scherm_zoom"):
        try:
            c["kiosk"]["zoom"] = max(0.5, min(3.0, float(vals["scherm_zoom"].replace(",", "."))))
        except ValueError:
            errs.append("scherm_zoom: getal, bv. 1.5")
    if changed("wifi_rol"):
        r = {"uit": "off"}.get(vals["wifi_rol"], vals["wifi_rol"])
        n["wifi"]["role"] = r
    if changed("hotspot_naam"):
        c["hotspot"]["ssid"] = vals["hotspot_naam"][:32]
    if vals.get("hotspot_wachtwoord"):
        if 8 <= len(vals["hotspot_wachtwoord"]) <= 63:
            c["hotspot"]["password"] = vals["hotspot_wachtwoord"]
        else:
            errs.append("hotspot_wachtwoord: 8 tot 63 tekens")
    if changed("noodhotspot_na_s"):
        try:
            c["hotspot"]["after_s"] = max(20, min(600, int(vals["noodhotspot_na_s"])))
        except ValueError:
            errs.append("noodhotspot_na_s: aantal seconden")
    if changed("ethernet"):
        n["ethernet"]["role"] = {"uit": "off"}.get(vals["ethernet"], vals["ethernet"])
    if changed("ethernet_ip"):
        if vals["ethernet_ip"].lower() == "dhcp":
            n["ethernet"]["ipv4"] = "dhcp"
        else:
            n["ethernet"].update(ipv4="static", address=vals["ethernet_ip"])
    for k, key in (("ethernet_gateway", "gateway"), ("ethernet_dns", "dns")):
        if changed(k):
            n["ethernet"][key] = vals[k]
    if changed("usb_modem"):
        n["usb"]["enabled"] = yes.get(vals["usb_modem"].lower(), True)
    if changed("wan_voorkeur"):
        n["wan"] = vals["wan_voorkeur"]
    if changed("updates_automatisch"):
        c["update"]["auto"] = yes.get(vals["updates_automatisch"].lower(), False)
    try:
        validate_network(n)
        save_conf(c)
        step("netwerk", lambda: apply_network(n))
        step("rol", lambda: apply_role(c))
    except Exception as e:
        errs.append(f"netwerk: {e}")
    for item in (vals.get("wifi_toevoegen") or "").split(";;"):
        if item.strip():
            ssid, _, pw = item.partition("|")
            try:
                wifi_add({"ssid": ssid.strip(), "password": pw.strip(), "security": "wpa-psk" if pw.strip() else "open"})
            except Exception as e:
                errs.append(f"wifi_toevoegen {ssid.strip()}: {e}")
    if vals.get("wifi_netwerk") and (changed("wifi_netwerk") or vals.get("wifi_wachtwoord")):
        threading.Thread(target=wifi_connect_job, args=(vals["wifi_netwerk"], vals.get("wifi_wachtwoord", ""), False),
                         daemon=True).start()
    srv = {}
    if vals.get("beheerderscode"):
        srv["admin_token"] = vals["beheerderscode"]
    if vals.get("publieke_server") and changed("publieke_server"):
        if not vals["publieke_server"].startswith(("https://", "http://")):
            errs.append("publieke_server: moet met https:// beginnen")
        else:
            srv["relay_url"] = vals["publieke_server"].rstrip("/")
    if vals.get("publieke_server_token"):
        srv["relay_token"] = vals["publieke_server_token"]
    if vals.get("beheer_op_afstand") and changed("beheer_op_afstand"):
        srv["remote_admin"] = yes.get(vals["beheer_op_afstand"].lower(), False)
    if srv:
        def code():
            req = urllib.request.Request(f"http://127.0.0.1:{APP_PORT}/api/settings", method="POST",
                                         data=json.dumps(srv).encode(),
                                         headers={"Content-Type": "application/json", "X-Agent-Key": AGENT_KEY})
            for _ in range(30):
                try:
                    urllib.request.urlopen(req, timeout=5)
                    return
                except OSError:
                    time.sleep(2)
            raise RuntimeError("server niet bereikbaar")
        threading.Thread(target=lambda: step("server-instellingen", code), daemon=True).start()
    if changed("ssh") or changed("ssh_wachtwoord_login"):
        d = {}
        if changed("ssh"):
            d["enabled"] = yes.get(vals["ssh"].lower(), True)
        if changed("ssh_wachtwoord_login"):
            d["passwordAuth"] = yes.get(vals["ssh_wachtwoord_login"].lower(), True)
        step("ssh", lambda: ssh_set(d))
    if vals.get("ssh_sleutel"):
        step("ssh_sleutel", lambda: _add_key("khzs", vals["ssh_sleutel"]))
    for e in errs:
        log("SD-bestand: " + e)
    return errs


# ---------------------------------------------------------------- gebruikers en SSH
USER_RX = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
KEY_RX = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh.com) [A-Za-z0-9+/=]{40,}( [^\r\n]{0,100})?$")
NL = "\n"
PROTECTED = {"root", "kiosk", "nobody"}
SSHD_CONF = "/etc/ssh/sshd_config.d/khzs.conf"


def users():
    import grp
    import pwd
    try:
        sudoers = set(grp.getgrnam("sudo").gr_mem)
    except KeyError:
        sudoers = set()
    out = []
    for u in pwd.getpwall():
        if u.pw_uid < 1000 or u.pw_uid >= 60000 or u.pw_name in PROTECTED:
            continue
        keys = 0
        try:
            keys = sum(1 for l in open(os.path.join(u.pw_dir, ".ssh", "authorized_keys")) if l.strip() and not l.startswith("#"))
        except OSError:
            pass
        rc, out_, _ = run(["passwd", "-S", u.pw_name], timeout=5)
        locked = len(out_.split()) > 1 and out_.split()[1] in ("L", "NP")
        out.append({"name": u.pw_name, "uid": u.pw_uid, "sudo": u.pw_name in sudoers, "sshKeys": keys,
                    "passwordLocked": locked, "system": u.pw_name == "khzs"})
    return out


def _check_user(name, must_exist=True):
    if not USER_RX.match(name or "") or name in PROTECTED:
        raise RuntimeError("ongeldige gebruikersnaam (kleine letters, cijfers, - en _; begint met een letter)")
    exists = any(u["name"] == name for u in users())
    if must_exist and not exists:
        raise RuntimeError(f"gebruiker {name} bestaat niet")
    if not must_exist and exists:
        raise RuntimeError(f"gebruiker {name} bestaat al")


def _set_password(name, pw):
    if len(pw or "") < 8:
        raise RuntimeError("wachtwoord: minstens 8 tekens")
    run(["chpasswd"], input=name + ":" + pw + NL, check=True)


def _add_key(name, key, replace=False):
    import pwd
    key = (key or "").strip()
    if not KEY_RX.match(key):
        raise RuntimeError("ongeldige SSH-sleutel (verwacht bv. 'ssh-ed25519 AAAA… naam')")
    u = pwd.getpwnam(name)
    d = os.path.join(u.pw_dir, ".ssh")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, "authorized_keys")
    lines = [] if replace or not os.path.exists(f) else [l.rstrip(NL) for l in open(f) if l.strip()]
    if key not in lines:
        lines.append(key)
    with open(f, "w") as fh:
        fh.write(NL.join(lines) + NL)
    os.chown(d, u.pw_uid, u.pw_gid)
    os.chown(f, u.pw_uid, u.pw_gid)
    os.chmod(d, 0o700)
    os.chmod(f, 0o600)


def user_action(p, d):
    name = d.get("username", "")
    if p == "/users/create":
        _check_user(name, must_exist=False)
        if not d.get("password") and not d.get("sshkey"):
            raise RuntimeError("geef een wachtwoord of een SSH-sleutel op")
        run(["useradd", "-m", "-s", "/bin/bash", "-c", (d.get("fullname") or "")[:60], name], check=True)
        try:
            if d.get("password"):
                _set_password(name, d["password"])
            if d.get("sshkey"):
                _add_key(name, d["sshkey"])
            if d.get("sudo"):
                run(["usermod", "-aG", "sudo", name], check=True)
        except Exception:
            run(["userdel", "-r", name])
            raise
        return {"ok": True, "message": f"gebruiker {name} aangemaakt"}
    _check_user(name)
    if p == "/users/password":
        _set_password(name, d.get("password"))
        return {"ok": True}
    if p == "/users/sudo":
        if d.get("on"):
            run(["usermod", "-aG", "sudo", name], check=True)
        else:
            if sum(1 for u in users() if u["sudo"]) <= 1:
                raise RuntimeError("dit is de laatste gebruiker met sudo-rechten")
            run(["gpasswd", "-d", name, "sudo"], check=True)
        return {"ok": True}
    if p == "/users/sshkey":
        _add_key(name, d.get("key"), replace=bool(d.get("replace")))
        return {"ok": True}
    if p == "/users/delete":
        if name == "khzs":
            raise RuntimeError("de gebruiker khzs is nodig voor de server")
        if any(u["name"] == name and u["sudo"] for u in users()) and sum(1 for u in users() if u["sudo"]) <= 1:
            raise RuntimeError("dit is de laatste gebruiker met sudo-rechten")
        run(["pkill", "-u", name])
        run(["userdel", "-r", name], check=True)
        return {"ok": True}
    raise RuntimeError("onbekende actie")


def ssh_status():
    rc, out, _ = run(["systemctl", "is-enabled", "ssh"], timeout=5)
    rc2, out2, _ = run(["systemctl", "is-active", "ssh"], timeout=5)
    pw = True
    try:
        pw = "PasswordAuthentication no" not in open(SSHD_CONF).read()
    except OSError:
        pass
    return {"enabled": out.strip() == "enabled", "active": out2.strip() == "active", "passwordAuth": pw}


def ssh_set(d):
    if "passwordAuth" in d:
        if not d["passwordAuth"] and not any(u["sshKeys"] and u["sudo"] for u in users()):
            raise RuntimeError("voeg eerst een SSH-sleutel toe aan een gebruiker met sudo-rechten")
        os.makedirs(os.path.dirname(SSHD_CONF), exist_ok=True)
        with open(SSHD_CONF, "w") as f:
            f.write("# beheerd door KHZS Timing" + NL + "PasswordAuthentication " + ("yes" if d["passwordAuth"] else "no")
                    + NL + "PermitRootLogin no" + NL)
        run(["systemctl", "reload", "ssh"])
    if "enabled" in d:
        run(["systemctl", "enable" if d["enabled"] else "disable", "--now", "ssh"], check=True)
    return ssh_status()


# ---------------------------------------------------------------- status
def status():
    c = load_conf()
    ap = ap_active()
    addrs = ip_addrs()
    dr = default_route()
    du = shutil.disk_usage("/")
    up = uplink()
    since = STATE["since_uplink"]
    try:
        mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
        memfree = round(mem.get("MemAvailable", 0) / 1024)
        memtot = round(mem.get("MemTotal", 0) / 1024)
    except (OSError, ValueError):
        memfree = memtot = None
    hs = dict(c["hotspot"])
    hs.pop("password", None)
    hs.update(active=bool(ap), iface=ap, clients=ap_clients(ap), reason=STATE.get("ap_reason") or "",
              hasPassword=bool(c["hotspot"].get("password")),
              countdown=None if up or ap else max(0, round(float(c["hotspot"]["after_s"]) - (time.time() - since))))
    upd = dict(c["update"])
    upd["token"] = bool(upd.get("token"))
    return {
        "hostname": socket.gethostname(), "version": app_version(), "uptime": round(time.time() - STATE["boot"]),
        "temp": read_temp(), "throttled": throttled(), "disk": {"free": du.free // 2**20, "total": du.total // 2**20},
        "mem": {"free": memfree, "total": memtot},
        "network": {"uplink": up, "internet": STATE.get("internet"), "addresses": addrs,
                    "gateway": dr and dr.get("gateway"), "gatewayDev": dr and dr.get("dev"),
                    "ethernet": dict(c["ethernet"], carrier=carrier("eth0")), "wifi": active_wifi(),
                    "wifiIfaces": wifi_ifaces(), "devices": devices(), "usbModems": usb_modems(), "savedWifi": saved_wifi(),
                    "wifiJob": STATE.get("wifi_job"), "lastWifi": STATE.get("last_wifi")},
        "routing": c["network"], "hotspot": hs, "kiosk": c["kiosk"],
        "device": dict(c["device"], display_key=bool(c["device"].get("display_key"))),
        "connectivity": STATE.get("connectivity"), "sdErrors": STATE.get("sd_errors") or [],
        "confirm": STATE["confirm"] and {"remaining": max(0, round(STATE["confirm"]["deadline"] - time.time())),
                                         "applied": STATE["confirm"]["applied"]},
        "lastRevert": STATE.get("last_revert"),
        "system": {"pending": STATE["system"]["pending"], "busy": STATE["system"]["busy"]}, "update": dict(upd, state={k: v for k, v in STATE["update"].items()}),
        "portal": {"running": bool(PORTAL.proc and PORTAL.proc.poll() is None)},
    }


# ---------------------------------------------------------------- HTTP
def ensure_key():
    """Gedeelde sleutel met de server (groep khzs mag lezen). Zonder sleutel geen toegang: zo kan een webpagina in
    de kiosk- of hotspot-browser de systeemdienst op 127.0.0.1 niet misbruiken."""
    global AGENT_KEY
    try:
        AGENT_KEY = open(KEY_FILE).read().strip()
    except OSError:
        AGENT_KEY = ""
    if len(AGENT_KEY) < 32:
        AGENT_KEY = secrets.token_hex(32)
        with open(KEY_FILE, "w") as f:
            f.write(AGENT_KEY + LF)
    run(["chown", "root:khzs", KEY_FILE])
    os.chmod(KEY_FILE, 0o640)


LF = chr(10)


class H(BaseHTTPRequestHandler):
    server_version = "khzs-agent"

    def _authorized(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False                                   # DNS-rebinding
        if self.headers.get("Origin"):
            return False                                   # verzoek vanuit een browserpagina
        k = self.headers.get("X-Agent-Key", "")
        return bool(AGENT_KEY) and hmac.compare_digest(k, AGENT_KEY)

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def _jbody(self):
        try:
            return json.loads(self._body() or b"{}")
        except ValueError:
            raise RuntimeError("ongeldige JSON")

    def do_GET(self):
        p = self.path.split("?")[0]
        if not self._authorized():
            return self._send(403, {"error": "geen toegang"})
        try:
            if p == "/status":
                return self._send(200, status())
            if p == "/wifi/scan":
                return self._send(200, {"networks": wifi_scan()})
            if p == "/dhcp/leases":
                return self._send(200, {"leases": dhcp_leases(), "reservations": load_conf()["network"].get("reservations", [])})
            if p == "/wifi/saved":
                return self._send(200, {"networks": wifi_saved_details()})
            if p == "/shares":
                return self._send(200, {"shares": shares_public()})
            if p.startswith("/console/"):
                try:
                    return self._send(200, console_api("GET", p, {}, urllib.parse.parse_qs(self.path.partition("?")[2])))
                except LookupError as e:
                    return self._send(404, {"error": str(e)})
            if p == "/shares/files":
                q = urllib.parse.parse_qs(self.path.partition("?")[2])
                return self._send(200, share_files(q.get("name", [""])[0], q.get("sub", [""])[0]))
            if p == "/portal/screenshot":
                if not (PORTAL.proc and PORTAL.proc.poll() is None):
                    PORTAL.start()
                png, href, title = PORTAL.screenshot()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("X-Portal-Url", urllib.parse.quote(href or "", safe=":/?&=%#"))
                self.send_header("X-Portal-Title", urllib.parse.quote(title or ""))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)
                return
            if p == "/update/log":
                return self._send(200, STATE["update"])
            if p == "/system/pending":
                return self._send(200, dict(STATE["system"], pending=STATE["system"]["pending"]
                                            if STATE["system"]["pending"] is not None else system_check()))
            if p == "/display":
                d = load_conf()["device"]
                return self._send(200, {"role": d["role"], "url": display_source(d), "auto": str(d["display_url"]).startswith("auto"),
                                        "backup": display_backup(d),
                                        "key": d.get("display_key", ""), "label": app_settings().get("device_label", ""),
                                        "name": socket.gethostname(), "addresses": ip_addrs()})
            if p == "/users":
                return self._send(200, {"users": users(), "ssh": ssh_status()})
            self._send(404, {"error": "onbekend"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        p = self.path.split("?")[0]
        if not self._authorized():
            return self._send(403, {"error": "geen toegang"})
        try:
            if p == "/update/upload":
                return self._send(200, install_release(self._body(), "upload"))
            d = self._jbody()
            c = load_conf()
            if p == "/wifi/connect":
                if not d.get("ssid"):
                    raise RuntimeError("geen netwerknaam")
                threading.Thread(target=wifi_connect_job, args=(d["ssid"], d.get("password", ""), d.get("hidden")),
                                 daemon=True).start()
                return self._send(200, {"ok": True, "message": "bezig met verbinden… (de hotspot kan even wegvallen)"})
            if p.startswith("/console/"):
                try:
                    return self._send(200, console_api("POST", p, d, {}))
                except LookupError as e:
                    return self._send(404, {"error": str(e)})
            if p == "/shares/save":
                return self._send(200, {"ok": True, "shares": shares_save(d.get("shares") or [])})
            if p == "/shares/mount":
                sh = next((x for x in c.get("shares", []) if x["name"] == d.get("name")), None)
                if not sh:
                    raise RuntimeError("onbekende netwerkschijf")
                mount_share(sh)
                return self._send(200, {"ok": True, "shares": shares_public()})
            if p == "/shares/unmount":
                unmount_share(d.get("name", ""))
                return self._send(200, {"ok": True, "shares": shares_public()})
            if p == "/wifi/add":
                return self._send(200, {"ok": True, "name": wifi_add(d)})
            if p == "/wifi/update":
                return self._send(200, {"ok": wifi_update(d)})
            if p == "/wifi/order":
                return self._send(200, {"ok": wifi_order(d.get("names") or [])})
            if p == "/wifi/up":
                threading.Thread(target=wifi_up_job, args=(d.get("name", ""),), daemon=True).start()
                return self._send(200, {"ok": True, "message": "bezig met verbinden… (de hotspot kan even wegvallen)"})
            if p == "/wifi/forget":
                nmcli("connection", "delete", d.get("name", ""), check=True)
                return self._send(200, {"ok": True})
            if p == "/network":
                n = c["network"]
                for k in ("wan", "fallback", "reservations"):
                    if k in d:
                        n[k] = d[k]
                for k in ("ethernet", "wifi", "usb"):
                    if isinstance(d.get(k), dict):
                        n[k].update(d[k])
                for k in ("ethernet", "hotspot"):
                    if isinstance((d.get("lan") or {}).get(k), dict):
                        n["lan"][k].update(d["lan"][k])
                msgs = network_change(n, c)
                return self._send(200, {"ok": True, "applied": msgs, "confirmWithin": CONFIRM_S})
            if p == "/network/confirm":
                network_confirm()
                return self._send(200, {"ok": True})
            if p == "/network/revert":
                network_revert("handmatig")
                return self._send(200, {"ok": True})
            if p == "/network/reset":
                reset_network()
                return self._send(200, {"ok": True})
            if p == "/internet":
                n = json.loads(json.dumps(c["network"]))
                n["internet"] = bool(d.get("on"))
                msgs = network_change(n, c)
                return self._send(200, {"ok": True, "applied": msgs, "confirmWithin": CONFIRM_S})
            if p == "/ethernet":
                c["ethernet"].update({k: d.get(k, "") for k in ("mode", "address", "gateway", "dns")})
                set_ethernet(c["ethernet"])
                save_conf(c)
                return self._send(200, {"ok": True})
            if p == "/hotspot":
                h = c["hotspot"]
                if "ssid" in d:
                    if not 1 <= len(d["ssid"]) <= 32:
                        raise RuntimeError("netwerknaam: 1 tot 32 tekens")
                    h["ssid"] = d["ssid"]
                if d.get("password") is not None and d.get("password") != "":
                    if not 8 <= len(d["password"]) <= 63:
                        raise RuntimeError("wachtwoord: 8 tot 63 tekens")
                    h["password"] = d["password"]
                for k in ("always",):
                    if k in d:
                        h[k] = bool(d[k])
                if "after_s" in d:
                    h["after_s"] = max(20, min(600, int(d["after_s"])))
                save_conf(c)
                if ap_active():
                    stop_ap()
                    start_ap(STATE.get("ap_reason") or "instellingen gewijzigd")
                return self._send(200, {"ok": True})
            if p == "/hotspot/start":
                return self._send(200, {"ok": start_ap("handmatig")})
            if p == "/hotspot/stop":
                stop_ap()
                return self._send(200, {"ok": True})
            if p == "/kiosk":
                k = c["kiosk"]
                for key in ("enabled", "url", "zoom", "mode", "rotate"):
                    if key in d:
                        k[key] = d[key]
                if not re.fullmatch(r"auto|\d{3,4}x\d{3,4}(@\d+(\.\d+)?Hz)?", str(k.get("mode") or "auto")):
                    raise RuntimeError("ongeldige resolutie")
                if int(k.get("rotate") or 0) not in (0, 90, 180, 270):
                    raise RuntimeError("draaiing: 0, 90, 180 of 270")
                if not str(k["url"]).startswith(("http://", "https://")):
                    raise RuntimeError("pagina moet met http:// of https:// beginnen")
                save_conf(c)
                apply_kiosk(k)
                return self._send(200, {"ok": True})
            if p == "/kiosk/restart":
                run(["systemctl", "restart", "khzs-kiosk.service"])
                return self._send(200, {"ok": True})
            if p == "/power":
                act = d.get("action")
                if act not in ("reboot", "poweroff"):
                    raise RuntimeError("onbekende actie")
                threading.Timer(2, lambda: run(["systemctl", act])).start()
                return self._send(200, {"ok": True})
            if p == "/update/settings":
                u = c["update"]
                for k in ("auto", "repo", "idle_min"):
                    if k in d:
                        u[k] = d[k]
                if d.get("token"):
                    u["token"] = d["token"].strip()
                if d.get("clearToken"):
                    u["token"] = ""
                save_conf(c)
                return self._send(200, {"ok": True})
            if p == "/update/cloud/check":
                return self._send(200, dict(cloud_check(), ok=True))
            if p == "/update/cloud/apply":
                return self._send(200, cloud_apply())
            if p == "/update/check":
                return self._send(200, gh_check())
            if p == "/update/apply":
                return self._send(200, gh_apply())
            if p == "/system/apply":
                return self._send(200, system_apply())
            if p == "/device":
                dv = c["device"]
                # maar één server per netwerk (anders sturen er twee naar dezelfde publieke server)
                if d.get("role") == "server" and dv.get("role") != "server" and not d.get("force"):
                    try:
                        peers = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}/api/peers", timeout=3).read()).get("peers", [])
                    except (OSError, ValueError):
                        peers = []
                    other = [p_ for p_ in peers if p_.get("role") == "server"]
                    if other:
                        o = other[0]
                        raise RuntimeError(f"er is al een server op dit netwerk: {o.get('label') or o.get('name')} ({o.get('ip')}). "
                                           "Zet die eerst op scherm of uit – er mag maar één server per netwerk zijn.")
                if "name" in d:
                    set_hostname(d["name"])
                if "display_backup_url" in d:
                    b = str(d.get("display_backup_url") or "").strip()
                    if b and b != "auto" and not b.startswith(("http://", "https://")):
                        raise RuntimeError("reservebron moet met http:// of https:// beginnen")
                    dv["display_backup_url"] = b
                for k in ("role", "display_url"):
                    if k in d:
                        dv[k] = d[k]
                if d.get("display_key"):
                    dv["display_key"] = d["display_key"]
                if d.get("clearDisplayKey"):
                    dv["display_key"] = ""
                if dv["role"] not in ("uit", "server", "scherm"):
                    raise RuntimeError("rol: uit, server of scherm")
                if dv["role"] == "scherm" and not str(dv["display_url"]).startswith(("http://", "https://", "auto")):
                    raise RuntimeError("bron moet met http:// of https:// beginnen")
                save_conf(c)
                apply_role(c)
                return self._send(200, {"ok": True})
            if p.startswith("/users/"):
                return self._send(200, user_action(p, d))
            if p == "/ssh":
                return self._send(200, ssh_set(d))
            if p == "/portal/start":
                PORTAL.start(d.get("url") or "http://neverssl.com/")
                return self._send(200, {"ok": True})
            if p == "/portal/stop":
                PORTAL.stop()
                return self._send(200, {"ok": True})
            if p == "/portal/click":
                PORTAL.click(float(d["x"]), float(d["y"]))
                return self._send(200, {"ok": True})
            if p == "/portal/type":
                PORTAL.type(str(d.get("text", "")))
                return self._send(200, {"ok": True})
            if p == "/portal/key":
                PORTAL.key(str(d.get("key", "Enter")))
                return self._send(200, {"ok": True})
            if p == "/portal/navigate":
                url = str(d.get("url", ""))
                if not url.startswith(("http://", "https://")):
                    url = "http://" + url
                PORTAL.navigate(url)
                return self._send(200, {"ok": True})
            self._send(404, {"error": "onbekend"})
        except Exception as e:
            self._send(400, {"error": str(e)})


# ---------------------------------------------------------------- root-console (beheer > Console)
import pty
import select
import termios

CONSOLES = {}


class ConsoleSession:
    """Root-shell in een PTY; uitvoer wordt gebufferd en via long-polling opgehaald (werkt ook via de hotspot)."""
    def __init__(self, sid, who):
        self.id, self.who = sid, who
        self.buf, self.base = bytearray(), 0
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
                if len(self.buf) > 512 * 1024:
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
            return {"pos": start + len(data), "data": base64.b64encode(data).decode(), "closed": self.closed}

    def write(self, data):
        self.last = time.time()
        os.write(self.fd, data)

    def resize(self, cols, rows):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(10, min(500, rows)), max(20, min(500, cols)), 0, 0))

    def close(self):
        self.closed = True
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.kill(self.pid, sig)
                time.sleep(0.2)
            except OSError:
                pass
        try:
            os.waitpid(self.pid, os.WNOHANG)
            os.close(self.fd)
        except OSError:
            pass
        with self.cond:
            self.cond.notify_all()


def console_reaper():
    while True:
        time.sleep(30)
        for sid, c in list(CONSOLES.items()):
            if c.closed or time.time() - c.last > 15 * 60 or time.time() - c.started > 4 * 3600:
                c.close()
                CONSOLES.pop(sid, None)
                log(f"console gesloten ({c.who})")


def console_api(method, p, d, q):
    if p == "/console/open" and method == "POST":
        if len(CONSOLES) >= 3:
            raise RuntimeError("te veel open consoles – sluit er eerst een")
        sid = secrets.token_urlsafe(24)
        CONSOLES[sid] = ConsoleSession(sid, d.get("who", "beheer"))
        if d.get("cols"):
            CONSOLES[sid].resize(int(d["cols"]), int(d.get("rows", 30)))
        log(f"root-console geopend via het beheer ({d.get('who', '?')})")
        return {"ok": True, "id": sid}
    parts = p.split("/")
    c = CONSOLES.get(parts[2]) if len(parts) > 3 else None
    if not c:
        raise LookupError("console bestaat niet (meer)")
    act = parts[3]
    if act == "read" and method == "GET":
        return dict(c.read(int(q.get("pos", ["0"])[0]), min(20.0, float(q.get("wait", ["20"])[0]))), ok=True)
    if act == "write":
        c.write(base64.b64decode(d.get("data", "")))
    elif act == "resize":
        c.resize(int(d.get("cols", 120)), int(d.get("rows", 32)))
    elif act == "close":
        c.close()
        CONSOLES.pop(c.id, None)
        log("root-console gesloten via het beheer")
    return {"ok": True}


# ---------------------------------------------------------------- netwerkschijven (alleen-lezen)
MNT = "/mnt"
CRED_DIR = "/etc/khzs"
SHARE_ERR = {}
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
UNC_RX = re.compile(r"^//[A-Za-z0-9._-]+/[^/\\:*?\"<>|]+(/[^\\:*?\"<>|]*)*$")


def norm_unc(path):
    p = (path or "").strip().replace("\\", "/")
    if p.startswith("smb://"):
        p = "//" + p[6:]
    if not p.startswith("//"):
        p = "//" + p.lstrip("/")
    return p.rstrip("/")


def share_mounted(name):
    target = os.path.join(MNT, name)
    try:
        for line in open("/proc/mounts"):
            f = line.split()
            if len(f) > 2 and f[1] == target and f[2] in ("cifs", "smb3"):
                return True
    except OSError:
        pass
    return False


def mount_share(sh):
    """Windows-share koppelen op /mnt/<naam>: ro (alleen-lezen), nobrl (geen byte-locks naar de server), cache=none
    (altijd de laatste versie), soft (geen hangende processen als de pc wegvalt)."""
    name = sh["name"]
    if not NAME_RX.match(name):
        raise RuntimeError("ongeldige naam (kleine letters, cijfers, - en _)")
    unc = norm_unc(sh["path"])
    if not UNC_RX.match(unc):
        raise RuntimeError("ongeldig pad, bv. \\\\SWIMTIME-PC\\Data")
    target = os.path.join(MNT, name)
    if share_mounted(name):
        run(["umount", "-l", target], 15)
    os.makedirs(target, exist_ok=True)
    os.makedirs(CRED_DIR, mode=0o700, exist_ok=True)
    cred = os.path.join(CRED_DIR, f"smb-{name}.cred")
    opts = ["ro", "nobrl", "cache=none", "actimeo=1", "soft", "echo_interval=10", "noperm",
            "file_mode=0444", "dir_mode=0555", "iocharset=utf8"]
    try:
        import pwd
        pw = pwd.getpwnam("khzs")
        opts += [f"uid={pw.pw_uid}", f"gid={pw.pw_gid}"]
    except KeyError:
        pass
    if sh.get("user"):
        fd = os.open(cred, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"username={sh['user']}\npassword={sh.get('password', '')}\n")
            if sh.get("domain"):
                f.write(f"domain={sh['domain']}\n")
        opts.append(f"credentials={cred}")
    else:
        opts.append("guest")
    if sh.get("vers"):
        opts.append(f"vers={sh['vers']}")
    rc, out, err = run(["mount", "-t", "cifs", unc, target, "-o", ",".join(opts)], 30)
    if rc != 0:
        msg = (err or out).strip()[:300]
        hint = ""
        if "error(13)" in msg:
            hint = " – gebruiker/wachtwoord fout of geen toegang"
        elif "error(2)" in msg:
            hint = " – share of map bestaat niet"
        elif "error(112)" in msg or "error(113)" in msg or "error(115)" in msg:
            hint = " – pc onbereikbaar (staat hij aan, zit hij op hetzelfde netwerk?)"
        elif "error(95)" in msg:
            hint = " – kies een andere SMB-versie (bv. 3.0 of 2.1)"
        SHARE_ERR[name] = msg + hint
        raise RuntimeError(f"koppelen mislukt: {msg}{hint}")
    SHARE_ERR.pop(name, None)
    log(f"netwerkschijf {name} gekoppeld (alleen-lezen): {unc}")
    return target


def unmount_share(name):
    target = os.path.join(MNT, name)
    if share_mounted(name):
        run(["umount", "-l", target], 15)
    return True


def shares_public(c=None):
    c = c or load_conf()
    out = []
    for sh in c.get("shares", []):
        out.append({"name": sh["name"], "path": sh["path"], "user": sh.get("user", ""), "domain": sh.get("domain", ""),
                    "vers": sh.get("vers", ""), "auto": sh.get("auto", True), "passwordSet": bool(sh.get("password")),
                    "mounted": share_mounted(sh["name"]), "mountpoint": os.path.join(MNT, sh["name"]),
                    "error": SHARE_ERR.get(sh["name"])})
    return out


def shares_save(items):
    """Lijst van shares opslaan (wachtwoord leeg = ongewijzigd). Verwijderde shares worden losgekoppeld."""
    c = load_conf()
    old = {sh["name"]: sh for sh in c.get("shares", [])}
    new = []
    for it in items:
        name = (it.get("name") or "").strip().lower()
        if not NAME_RX.match(name):
            raise RuntimeError(f"ongeldige naam '{name}' (kleine letters, cijfers, - en _)")
        unc = norm_unc(it.get("path"))
        if not UNC_RX.match(unc):
            raise RuntimeError(f"ongeldig pad voor {name}")
        if it.get("vers") not in (None, "", "1.0", "2.0", "2.1", "3.0", "3.1.1"):
            raise RuntimeError("ongeldige SMB-versie")
        sh = {"name": name, "path": unc, "user": (it.get("user") or "").strip(), "domain": (it.get("domain") or "").strip(),
              "vers": it.get("vers") or "", "auto": bool(it.get("auto", True)),
              "password": it.get("password") or (old.get(name) or {}).get("password", "")}
        new.append(sh)
    if len({s["name"] for s in new}) != len(new):
        raise RuntimeError("elke naam mag maar één keer voorkomen")
    for name in set(old) - {s["name"] for s in new}:
        unmount_share(name)
        try:
            os.remove(os.path.join(CRED_DIR, f"smb-{name}.cred"))
        except OSError:
            pass
    c["shares"] = new
    save_conf(c)
    return shares_public(c)


def share_files(name, sub=""):
    """Bestanden in een gekoppelde share (om de database te kiezen). Enkel lezen."""
    if not share_mounted(name):
        raise RuntimeError("niet gekoppeld")
    root = os.path.realpath(os.path.join(MNT, name))
    cur = os.path.realpath(os.path.join(root, sub.strip("/")))
    if not (cur == root or cur.startswith(root + "/")):
        raise RuntimeError("ongeldige map")
    dirs, files = [], []
    with os.scandir(cur) as it:
        for e in sorted(it, key=lambda e: e.name.lower()):
            if e.name.startswith((".", "~$")):
                continue
            try:
                if e.is_dir():
                    dirs.append(e.name)
                elif e.is_file():
                    st = e.stat()
                    files.append({"name": e.name, "size": st.st_size, "mtime": st.st_mtime,
                                  "db": e.name.lower().endswith((".mdb", ".accdb"))})
            except OSError:
                continue
    rel = os.path.relpath(cur, root)
    return {"path": "" if rel == "." else rel, "mountpoint": root, "dirs": dirs[:300], "files": files[:500]}


def shares_watch():
    """Automatisch koppelen bij opstart en opnieuw proberen zolang het niet lukt (netwerk kan later komen)."""
    time.sleep(10)
    while True:
        for sh in load_conf().get("shares", []):
            if sh.get("auto", True) and not share_mounted(sh["name"]):
                try:
                    mount_share(sh)
                except Exception as e:
                    if DEBUG:
                        log(f"netwerkschijf {sh['name']}: {e}")
        time.sleep(30)


# ---------------------------------------------------------------- opstartscherm
ROLE_TXT = {"uit": "beheer (live timing uit)", "server": "live timing-server", "scherm": "scherm"}


def iface_label(name):
    if name.startswith(("eth", "end", "enp")):
        return "Ethernet"
    if name == "uap0":
        return "Hotspot"
    if name.startswith("wl"):
        return "Wi-Fi"
    if name.startswith(("usb", "wwan", "enx")):
        return "4G/USB"
    return name


def boot_message():
    """Eén regel voor het opstartscherm: rol · netwerk · adres · versie."""
    c = load_conf()
    parts = [app_settings().get("device_label") or "", ROLE_TXT.get(c["device"].get("role"), c["device"].get("role") or "")]
    ap = ap_active()
    nets = []
    for name, addrs in sorted(ip_addrs().items()):
        if name == "lo" or not addrs:
            continue
        lbl = "Hotspot " + c["hotspot"]["ssid"] if name == ap else iface_label(name)
        nets.append(f"{lbl} {addrs[0].split('/')[0]}")
    if nets:
        parts += nets[:2]
        parts.append(f"http://{socket.gethostname()}.local")
    else:
        parts.append(f"wachten op netwerk – hotspot {c['hotspot']['ssid']} volgt na 1 minuut")
    v = app_version()
    if v:
        parts.append("versie " + v)
    return "  ·  ".join(p for p in parts if p)


def bootscreen_feed():
    """Zolang het opstartscherm (Plymouth) draait: rol, netwerk en adres tonen (max. 3 minuten)."""
    if not shutil.which("plymouth"):
        return
    last, end = None, time.time() + 180
    while time.time() < end:
        if run(["plymouth", "--ping"], timeout=3)[0] != 0:
            return
        try:
            msg = boot_message()
        except Exception as e:
            msg = None
            log(f"opstartscherm: {e}")
        if msg and msg != last:
            run(["plymouth", "display-message", "--text=" + msg], timeout=3)
            last = msg
        time.sleep(1.5)


def main():
    os.makedirs(DATA, exist_ok=True)
    threading.Thread(target=bootscreen_feed, daemon=True).start()
    ensure_key()
    c = load_conf()
    if not os.path.exists(CONF):
        save_conf(c)
    try:
        apply_kiosk(c["kiosk"]) if not os.path.exists(KIOSK_ENV) else None
    except Exception as e:
        log(f"kiosk: {e}")
    try:
        read_sd_file()
    except Exception as e:
        log(f"SD-bestand: {e}")
    try:
        if not os.path.exists(ROLE_ENV):
            apply_role(load_conf())
    except Exception as e:
        log(f"rol: {e}")

    def healthy():
        time.sleep(60)
        try:
            with open(FAILS, "w") as f:
                f.write("0")
        except OSError:
            pass
        system_check()
    for t in (netwatch, auto_updater, PORTAL.idle_reaper, healthy, display_watch, shares_watch, console_reaper, kiosk_label):
        threading.Thread(target=t, daemon=True).start()
    srv = ThreadingHTTPServer(LISTEN, H)
    srv.daemon_threads = True
    log(f"khzs-agent luistert op {LISTEN[0]}:{LISTEN[1]}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
