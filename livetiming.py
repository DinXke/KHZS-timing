#!/usr/bin/env python3
"""Read-only live timing voor ALGE SwimTime.

Luistert naar de scorebord-broadcast van SwimTime (UDP/26, ASCII, tab-gescheiden
`sleutel<TAB>waarde`, afgesloten met CR) en serveert een live webpagina via
Server-Sent Events. Enkel Python-standaardbibliotheek.

    python livetiming.py                       # live op UDP/26, web op :8080
    python livetiming.py --replay capture.pcapng --speed 4
    python livetiming.py --replay raw.log
"""
import argparse
import base64
import hmac
import io
import ipaddress
import hashlib
import http.client
import urllib.parse
import secrets as _secrets
import json
import os
import re
import shutil
import struct
import queue
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.1.9"
BASE = os.path.dirname(os.path.abspath(__file__))          # code (op de Pi: /opt/khzs/current)
DATA = os.environ.get("KHZS_DATA") or BASE                 # gegevens (op de Pi: /var/lib/khzs) – blijft bij updates
ROLE = os.environ.get("KHZS_ROLE", "server")               # server | display | off  (op de Pi via het beheer)
STATIC = os.path.join(BASE, "static")
TSHARK = r"C:\Program Files\Wireshark\tshark.exe"

# Alle drempels/opties staan in settings.json en zijn aanpasbaar via /settings.
# Waarden in seconden tenzij anders vermeld.
DEFAULT_SETTINGS = {
    "manual_threshold_s": 1.5,     # tijd komt later binnen dan start + tijd = manueel ingegeven (handtijd)
    "clear_grace_s": 3.0,          # LaneTime -1 pas toepassen als er geen nieuwe waarde volgt
    "stale_after_s": 10.0,         # zonder pakketten = geen verbinding met tijdsysteem
    "history_max": 200,            # aantal bewaarde reeksen
    "udp_interface": "",           # SwimTime-ontvangst enkel op deze verbinding (Linux, bv. eth0); leeg = alle
    "udp_allow_from": "",          # enkel pakketten van deze afzender(s), bv. 192.168.0.188 of 192.168.0.0/24; leeg = iedereen
    "feed_port": 2626,             # TCP-invoer voor de simulator (naast UDP/26); 0 = uit
    "feed_bind": "127.0.0.1",      # 127.0.0.1 = enkel deze pc; 0.0.0.0 = ook een simulator op een andere pc
    "show_reaction": True,         # publiek: reactietijden tonen
    "show_manual": True,           # publiek: manuele tijden rood markeren
    "show_suspect": True,          # publiek: verdachte tijden oranje markeren
    "show_clock": True,            # publiek: lopende tijd tonen
    "official_split_distances": "50,100,400,800",  # m: officiele tussentijden bij enkelvoudige slagen
    "public_all_splits": True,     # publiek: alle tussentijden per lap (50 m) i.p.v. enkel officiele afstanden
    "hold_previous_until_start": False,  # True: afgelopen reeks groot houden tot de volgende start (startlijst in balk)
    "overview_heats": 1,           # jury: aantal vorige reeksen rechts
    "jury_split_left_pct": 67,     # jury: breedte linkerkant (huidige reeks) in %, rest = vorige reeksen
    "public_split_layout": True,   # publiek: ook huidige reeks links + vorige reeks(en) rechts (zonder fouten)
    "splash_enabled": True,        # kort geanimeerd tussenscherm bij een nieuwe reeks (publiek)
    "splash_seconds": 5.0,         # duur van het tussenscherm
    "callroom_splash_seconds": 8.0,  # duur van het oproepkamer-tussenscherm ("naar de oproepkamer")
    "splash_jury": True,           # ook op de jurypagina tonen
    "splash_callroom": True,       # ook in de oproepkamer tonen
    "splash_swim_seconds": 2.0,    # duur van de zwemmer-animatie (links -> rechts) binnen het tussenscherm
    "pool_lanes": 0,               # banen in het bad: 0 = automatisch (uit de startlijsten), anders 6/8/10 (10 = 0–9)
    "callroom_heats": 4,           # oproepkamer: aantal aankomende reeksen links
    "callroom_splash_heat": 0,     # oproepkamer-tussenscherm: welke aankomende reeks (0 = de laatste in de lijst = net binnen, 1 = volgende, ...)
    "callroom_next_big": True,     # oproepkamer: eerstvolgende reeks groter bovenaan
    "callroom_next_scale": 1.6,    # oproepkamer: vergroting van de eerstvolgende reeks
    "callroom_show_previous": False,  # oproepkamer: vorige reeks rechts tonen (onder de huidige)
    "callroom_split_left_pct": 60, # oproepkamer: breedte linkerkant (aankomende reeksen) in %
    "db_source_path": r"W:\2026_PK.mdb",  # SwimTime-database; wordt ENKEL gekopieerd op commando (Synchroniseren)
    "db_access_mode": "kopie",     # kopie | rechtstreeks (kastje: alleen-lezen op een gekoppelde netwerkschijf)
    "db_smb_user": "",             # kastje (Linux): gebruiker voor een Windows-share (\\pc\share\x.mdb); leeg = gast
    "db_smb_password": "",         # wachtwoord voor die share (geheim)
    "db_smb_domain": "",           # domein/werkgroep (optioneel)
    "db_auto_after_heat": True,    # na elke reeks automatisch een kopie nemen
    "db_after_heat_delay_s": 10.0, # wachttijd na het laden van de volgende startlijst
    "db_interval_s": 0.0,          # >0: elke X s een kopie als het bestand gewijzigd is (0 = uit) = "Live database"
    "db_live_interval_s": 1.0,     # live database: elke X s controleren of het bestand gewijzigd is
    "relay_min_takeover_s": -0.03, # aflossing: overnametijd lager dan dit = te vroege wissel
    "max_peer_panel_diff_s": 0.30, # jury: verschil peer/paneel groter dan dit = markeren
    "show_peer": True,             # jury: handtijd (peer) klein onder de paneeltijd
    "admin_token": "",             # beheerderscode om instellingen te wijzigen buiten localhost
    "trust_localhost": True,       # laptop: dit toestel zelf is beheerder; kastje: False = ook het HDMI-scherm moet de code geven
    "relay_url": "",               # zend-modus: publieke server om naar door te sturen (https://...)
    "relay_token": "",             # gedeeld geheim tussen laptop en publieke server
    "jury_password": "",           # relay-modus: wachtwoord voor /jury en /settings (oproepkamer is publiek)
    "cloud_admin_password": "",
    "https_port": 0,               # versleutelde toegang (zelfondertekend certificaat); 0 = op het kastje 443, laptop uit; -1 = uit
    "device_label": "",            # vrije naam van dit toestel, bv. "Scherm cafetaria" (zichtbaar in beheer, Kastjes, scherm)
    "remote_admin": False,         # beheer op afstand: dit toestel bereikbaar via https://<publieke server>/kastje/<naam>/    # relay-modus: beheerwachtwoord (infoscherm, agenda, statistieken) – zonder kastje
    # ingebouwde simulator (beheer > Simulator): nep-wedstrijd zonder SwimTime, ook op het kastje
    "sim_speed": 1.0,              # 1 = echte tijd
    "sim_events": 12,              # aantal wedstrijden
    "sim_per_event": 18,           # gemiddeld aantal deelnemers per wedstrijd
    "sim_lanes": 8,
    "sim_loop": True,              # na de laatste reeks opnieuw beginnen (automatisch verloop)
    "sim_pause": 25.0,             # s tussen startlijst en start (automatisch verloop)
    "sim_result_time": 15.0,       # s dat de uitslag blijft staan
    "sim_manual": False,           # True = geen automatisch verloop (zelf bedienen)
    "sim_manual_chance": 0.06,     # kans op een manuele (late) eindtijd
    "sim_extra_touch_chance": 0.15,  # kans per zwemmer op een extra tik tijdens de race
    "sim_dns_chance": 0.03,        # kans dat een zwemmer niet start
    "sim_seed": 0,                 # 0 = telkens een ander programma
    "sim_meet_name": "SIMULATIE – Testwedstrijd",
    "sim_autostart": False,        # simulator meteen starten als de server start (demo-kastje)
    "sim_stop_on_real": True,      # echte SwimTime-gegevens stoppen de simulator meteen
}
SETTINGS = dict(DEFAULT_SETTINGS)
SECRET_KEYS = ("admin_token", "relay_token", "jury_password", "db_smb_password", "cloud_admin_password")
SETTINGS_FILE = os.path.join(DATA, "settings.json")
PUBLIC_SETTINGS = ("pool_lanes", "callroom_splash_heat", "callroom_splash_seconds", "splash_enabled", "splash_seconds", "splash_jury", "splash_callroom", "splash_swim_seconds", "public_split_layout", "jury_split_left_pct", "callroom_heats", "callroom_next_big", "callroom_next_scale", "callroom_show_previous", "callroom_split_left_pct", "show_reaction", "show_manual", "show_suspect", "show_clock", "overview_heats", "show_peer", "max_peer_panel_diff_s")


def load_settings(path):
    global SETTINGS_FILE
    SETTINGS_FILE = path
    try:
        with open(path, encoding="utf-8-sig") as f:   # -sig: ook bestanden met BOM (PowerShell 5)
            SETTINGS.update({k: v for k, v in json.load(f).items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    save_settings()


def save_settings():
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(SETTINGS, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def update_settings(new):
    """Valideert en past toe; geeft lijst met fouten terug."""
    errors = []
    for k, v in new.items():
        if k not in DEFAULT_SETTINGS:
            errors.append(f"onbekende instelling {k}")
            continue
        d = DEFAULT_SETTINGS[k]
        try:
            if isinstance(d, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "ja", "on")
            elif isinstance(d, int):
                v = int(v)
            elif isinstance(d, float):
                v = float(v)
            else:
                v = str(v)
        except (TypeError, ValueError):
            errors.append(f"ongeldige waarde voor {k}")
            continue
        SETTINGS[k] = v
    if not errors:
        save_settings()
    return errors


def fmt_t(v):
    """1/10000 s -> 'ss.hh' / 'm:ss.hh', afgekapt op honderdsten (zoals officiële zwemtijden)."""
    h = int(v) // 100
    m, h = divmod(h, 6000)
    return f"{m}:{h // 100:02d}.{h % 100:02d}" if m else f"{h // 100}.{h % 100:02d}"

_SPEED = 1.0
_T_START = time.monotonic()


def now_fn():
    """Monotone klok; bij replay versneld zodat drempels in capture-tijd gelden."""
    return _T_START + (time.monotonic() - _T_START) * _SPEED


def tod(v):
    """1/10000 s sinds middernacht -> 'HH:MM'."""
    try:
        s = int(v) // 10000
    except (TypeError, ValueError):
        return ""
    return f"{s // 3600:02d}:{s // 60 % 60:02d}"


def alge_date(v):
    """(jaar<<16)|(maand<<8)|dag -> 'YYYY-MM-DD'."""
    try:
        v = int(v)
        return f"{v >> 16:04d}-{(v >> 8) & 0xFF:02d}-{v & 0xFF:02d}"
    except (TypeError, ValueError):
        return ""


def to_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_discipline(disc, distance=0):
    """'100m Schoolslag dames' / '4x50m Vrije slag' / '200m Wisselslag' -> dict."""
    d = (disc or "").lower()
    out = {"distance": distance or 0, "relay": False, "legs": 1, "leg": distance or 0, "medley": False}
    m = re.search(r"(\d+)\s*[x×]\s*(\d+)\s*m?", d)
    if m:
        out.update(relay=True, legs=int(m.group(1)), leg=int(m.group(2)))
        out["distance"] = out["distance"] or out["legs"] * out["leg"]
    else:
        m = re.search(r"(\d+)\s*m", d)
        if m and not out["distance"]:
            out["distance"] = out["leg"] = int(m.group(1))
        if "aflos" in d or "relay" in d:
            out["relay"] = True
    out["medley"] = "wissel" in d or "medley" in d or "4 slagen" in d
    if not out["relay"]:
        out["leg"] = out["distance"]
    return out


def total_distance(disc, distance):
    """Totale wedstrijdafstand. SwimTime stuurt bij aflossingen DistanceM = afstand per zwemmer (bv. 50 bij 4x50)."""
    d = to_int(distance, 0) or 0
    p = parse_discipline(disc, 0)
    if p["relay"] and p["legs"] > 1 and p["leg"]:
        return max(d, p["legs"] * p["leg"])
    return d


def official_distances(disc, distance):
    """Set van afstanden (m) waarop een tussentijd officieel kan tellen."""
    p = parse_discipline(disc, distance)
    try:
        allowed = {int(x) for x in str(SETTINGS["official_split_distances"]).replace(";", ",").split(",") if x.strip()}
    except ValueError:
        allowed = {50, 100, 400, 800}
    if p["relay"]:
        # enkel de eerste zwemmer; diens eindtijd (leg) telt ook
        return ({d for d in allowed if d < p["leg"]} | {p["leg"]}) - {p["distance"]}
    if p["medley"]:
        return {p["distance"] // 4} if p["distance"] in (200, 400) else set()
    return {d for d in allowed if d < p["distance"]}


def enrich_heat(item):
    """Voegt per split 'official' toe en een lijst 'issues' (fouten/opmerkingen) voor een reeks."""
    ev, heat = item.get("event", {}), item.get("heat", {})
    dist = total_distance(ev.get("discipline", ""), heat.get("distance"))
    heat = dict(heat, distance=dist)
    item["heat"] = heat
    laps = heat.get("laps") or 0
    off = official_distances(ev.get("discipline", ""), dist)
    issues = []
    lanes = []
    m = re.search(r"(\d+)\s*$", heat.get("name") or "")
    heat_nr = m.group(1) if m else str(heat.get("number", ""))   # naam is betrouwbaarder (HeatNumber-pakket kan verloren gaan)
    db = None if item.get("simulated") else DB_DATA.get((str(ev.get("number", "")), heat_nr))
    per = dist / laps if laps else 0
    maxdiff = float(SETTINGS["max_peer_panel_diff_s"]) * 10000
    for l in item.get("lanes", []):
        l = dict(l)
        # 'suspect' enkel als SwimTime het zelf doorgeeft; oude eigen analyses uit history negeren
        # afstandslabels herrekenen op de totale afstand (oude items kunnen 12/25/38 m hebben bij aflossingen)
        l["splits"] = [dict(s, suspect=None, distance=(round(per * s["lap"]) if per and s.get("lap") else s.get("distance")))
                       for s in l.get("splits", [])]
        l["suspect"] = None
        dbl = db["lanes"].get(int(l["lane"])) if db else None
        if dbl:
            have = {s["lap"]: s for s in l["splits"]}
            for lap, d in dbl["laps"].items():
                sp = have.get(lap)
                if sp is None:
                    # ontbrak in de broadcast (bv. verloren Wi-Fi-pakket): aanvullen uit de database
                    t = d["manual"] if d["manual"] is not None else d["panel"]
                    if t is None:
                        continue
                    sp = {"lap": lap, "distance": round(per * lap) if per else None, "time": t, "rank": None,
                          "manual": False, "overwrittenFrom": None, "suspect": None, "fromDb": True}
                    l["splits"].append(sp)
                sp["panel"], sp["peer"] = d["panel"], d["peer"]
                sp["peerDiff"] = (d["peer"] - d["panel"]) if (d["peer"] is not None and d["panel"] is not None) else None
                sp["peerDiffExceeded"] = sp["peerDiff"] is not None and abs(sp["peerDiff"]) > maxdiff
                if d["manual"] is not None:
                    sp["manual"] = True
                    sp["manualSource"] = "swimtime"
            l["splits"].sort(key=lambda x: x["lap"])
            if l.get("reaction") is None and dbl.get("reaction") is not None:
                l["reaction"] = dbl["reaction"]
            if laps and laps in {x["lap"] for x in l["splits"]}:
                fin = next(x for x in l["splits"] if x["lap"] == laps)
                l["final"], l["finalManual"], l["finished"] = fin["time"], bool(fin.get("manual")), True
        for sp in l["splits"]:
            sp["official"] = sp.get("distance") in off and sp.get("distance") != dist
        l["db"] = bool(dbl)
        pdisc = parse_discipline(ev.get("discipline", ""), dist)
        if dbl and dbl.get("members") and pdisc["relay"] and laps:
            legs = pdisc["legs"] or len(dbl["members"])
            lpl = max(1, round(laps / legs))          # laps per deelnemer
            bylap = {x["lap"]: x for x in l["splits"]}
            members, prev_cum = [], 0
            minto = float(SETTINGS["relay_min_takeover_s"]) * 10000
            for pos in range(1, legs + 1):
                m = dict(dbl["members"].get(pos, {}), position=pos)
                end = bylap.get(pos * lpl)
                m["cum"] = end["time"] if end else None
                m["leg"] = (end["time"] - prev_cum) if (end and prev_cum is not None) else None
                m["splits"] = [dict(x, legDistance=round(per * (x["lap"] - (pos - 1) * lpl)) if per else None)
                               for x in l["splits"] if (pos - 1) * lpl < x["lap"] <= pos * lpl]
                if pos == 1:
                    m["reaction"] = l.get("reaction")
                    m["takeover"] = None
                else:
                    prev_end = bylap.get((pos - 1) * lpl)
                    touch = prev_end.get("panel") if prev_end and prev_end.get("panel") is not None else (prev_end or {}).get("time")
                    blocks = (dbl.get("blocks") or {}).get((pos - 1) * lpl) or []
                    m["takeover"] = min((b - touch for b in blocks), key=abs) if (touch is not None and blocks) else None
                    m["takeoverExceeded"] = m["takeover"] is not None and m["takeover"] < minto
                members.append(m)
                prev_cum = m["cum"]
            l["team"] = dbl.get("team")
            l["members"] = members
        lanes.append(l)
        who = f"Baan {l['lane']}" + (f" – {l.get('firstName', '')} {l.get('lastName', '')}".rstrip() if l.get("lastName") else "")
        named = bool(l.get("firstName") or l.get("lastName"))
        for s in l["splits"]:
            label = "eindtijd" if s.get("distance") == dist else f"{s.get('distance')}m"
            if s.get("manual"):
                src = " (keuze in SwimTime)" if s.get("manualSource") == "swimtime" else " (geen paneeltijd, tikking manueel opgehoogd)"
                issues.append({"lane": l["lane"], "severity": "fout", "type": "manueel",
                               "text": f"{who}: {label} {fmt_t(s['time'])} manueel opgehoogd{src}"})
            if s.get("peerDiffExceeded"):
                issues.append({"lane": l["lane"], "severity": "fout", "type": "verschil peer/paneel",
                               "text": f"{who}: {label} verschil peer/paneel {abs(s['peerDiff']) / 10000:.2f} s "
                                       f"(paneel {fmt_t(s['panel'])}, peer {fmt_t(s['peer'])})"})
            if s.get("suspect"):
                issues.append({"lane": l["lane"], "severity": "fout", "type": "verdacht",
                               "text": f"{who}: {label} {fmt_t(s['time'])} verdacht ({s['suspect']})"})
            if s.get("overwrittenFrom"):
                issues.append({"lane": l["lane"], "severity": "info", "type": "gecorrigeerd",
                               "text": f"{who}: {label} gecorrigeerd van {fmt_t(s['overwrittenFrom'])} naar {fmt_t(s['time'])}"})
        for m in l.get("members") or []:
            if m.get("takeoverExceeded"):
                issues.append({"lane": l["lane"], "severity": "fout", "type": "te vroege wissel",
                               "text": f"{who}: wissel naar {m.get('firstName', '')} {m.get('lastName', '')} "
                                       f"{m['takeover'] / 10000:+.2f} s (te vroeg)"})
        if named and laps:
            have = {s["lap"] for s in l["splits"]}
            last = max(have) if have else 0
            missing = [x for x in range(1, last) if x not in have]
            if missing:
                per = dist / laps if laps else 0
                issues.append({"lane": l["lane"], "severity": "waarschuwing", "type": "split ontbreekt",
                               "text": f"{who}: geen tussentijd op " + ", ".join(f"{round(per * x)}m" for x in missing)})
            if not l.get("finished"):
                issues.append({"lane": l["lane"], "severity": "waarschuwing", "type": "geen eindtijd",
                               "text": f"{who}: geen eindtijd" + (f" (laatste: {round(dist / laps * last)}m)" if last else "")})
    for lap in {x["lap"] for l in lanes for x in l["splits"]}:
        ts = [x["time"] for l in lanes for x in l["splits"] if x["lap"] == lap]
        for l in lanes:
            for x in l["splits"]:
                if x["lap"] == lap:
                    x["rank"] = 1 + sum(1 for t in ts if t < x["time"])
    order = {"fout": 0, "waarschuwing": 1, "info": 2}
    issues.sort(key=lambda x: (order[x["severity"]], x["lane"]))
    item["lanes"] = lanes
    item["issues"] = issues
    item["officialDistances"] = sorted(off)
    return item


class Lane:
    def __init__(self, n):
        self.n = n
        self.info = {}
        self.reaction = None
        self.splits = {}          # lap -> dict(time, rank, manual, overwrittenFrom)
        self.clear_at = None      # monotonic tijd van een openstaande -1

    def has_content(self):
        return bool(self.info.get("FirstName") or self.info.get("LastName") or self.splits or self.reaction)


STATE_SOURCE = {"sim": False, "name": None}   # actieve bron (socket / simulator), gezet door Intake


class Heat:
    def __init__(self, event, heat):
        self.event = dict(event)
        self.heat = dict(heat)
        self.lanes = {}
        self.t0 = None            # monotonic tijdstip van de start (geschat uit RunningTime)
        self.started = False
        self.simulated = bool(STATE_SOURCE.get("sim"))

    def lane(self, n):
        if n not in self.lanes:
            self.lanes[n] = Lane(n)
        return self.lanes[n]

    @property
    def laps(self):
        return to_int(self.heat.get("Laps"), 0) or 0

    @property
    def distance(self):
        return total_distance(self.event.get("Discipline", ""), self.heat.get("DistanceM"))

    def has_times(self):
        return any(l.splits for l in self.lanes.values())


class State:
    def __init__(self, history_file):
        self.lock = threading.RLock()
        self.meet = {}
        self.session = {}
        self.event = {}
        self.heat_info = {}
        self.records = {}
        self.layout = ""
        self.clock_status = "idle"
        self.clock_rt = None
        self.prev_rt = None
        self.cur = None
        self.prev_heat = None     # afgelopen reeks: blijft groot in beeld tot de volgende start
        self.history = []
        self.history_file = history_file
        self.last_packet = 0.0
        self.version = 0
        self.history_version = 0
        self._load_history()

    # ---------- persistentie ----------
    def _load_history(self):
        try:
            with open(self.history_file, encoding="utf-8-sig") as f:
                self.history = json.load(f)
        except (OSError, ValueError):
            self.history = []

    def _save_history(self):
        tmp = self.history_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False)
            os.replace(tmp, self.history_file)
        except OSError as e:
            print("history opslaan mislukt:", e)

    # ---------- verwerking ----------
    def changed(self):
        self.version += 1

    def _switch_heat(self):
        ev = (self.event.get("EventName"), self.heat_info.get("HeatNumber"), self.heat_info.get("HeatName"))
        if self.cur and (self.cur.event.get("EventName"), self.cur.heat.get("HeatNumber"), self.cur.heat.get("HeatName")) == ev:
            self.cur.event.update(self.event)
            self.cur.heat.update(self.heat_info)
            return
        # enkel reeksen die live gestart zijn (klok liep); bladeren door oude reeksen in SwimTime telt niet
        if self.cur and self.cur.started and self.cur.has_times() and self.cur.event.get("EventName"):
            self._archive(self.cur)
            self.prev_heat = self.cur
            if not self.cur.simulated:
                db_after_heat()
        self.cur = Heat(self.event, self.heat_info)

    def drop_simulated(self):
        """Echte data komt weer binnen: simulatiereeksen uit de historiek en de huidige reeks wissen."""
        with self.lock:
            before = len(self.history)
            self.history = [x for x in self.history if not x.get("simulated")]
            if self.cur and self.cur.simulated:
                self.cur = None
            if self.prev_heat is not None and self.prev_heat.simulated:
                self.prev_heat = None
            removed = before - len(self.history)
            if removed:
                self.history_version += 1
                self._save_history()
            self.changed()
        SIM_SCHEDULE.clear()
        print(f"simulatie gewist ({removed} reeksen uit de historiek)")

    def _archive(self, h):
        item = {"event": self._event_json(h.event), "heat": self._heat_json(h.heat, h.event.get("Discipline", "")),
                "finishedAt": datetime.now().strftime("%H:%M"), "lanes": self._lanes_json(h)}
        if h.simulated:
            item["simulated"] = True
        key = (item["event"]["number"], item["heat"]["number"])
        self.history = [x for x in self.history if (x["event"]["number"], x["heat"]["number"]) != key]
        self.history.insert(0, item)
        del self.history[int(SETTINGS["history_max"]):]
        self.history_version += 1
        self._save_history()

    def handle(self, raw, now):
        try:
            txt = raw.decode("utf-8")      # SwimTime stuurt UTF-8
        except UnicodeDecodeError:
            txt = raw.decode("latin-1")
        txt = txt.rstrip("\r\n\x00")
        f = txt.split("\t")
        if not f or not f[0]:
            return
        with self.lock:
            self.last_packet = now
            sec = f[0]
            if sec == "Time" and len(f) >= 4:
                self._time(f, now)
            elif sec == "Meet" and len(f) >= 3:
                self.meet[f[1]] = f[2]; self.changed()
            elif sec == "Session" and len(f) >= 3:
                self.session[f[1]] = f[2]; self.changed()
            elif sec == "Event" and len(f) >= 3:
                self.event[f[1]] = f[2]
                if self.cur and self.cur.event.get("EventName") == self.event.get("EventName"):
                    self.cur.event[f[1]] = f[2]
                self.changed()
            elif sec == "Heat" and len(f) >= 3:
                self.heat_info[f[1]] = f[2]
                if f[1] == "HeatName":     # HeatNumber komt ervoor, HeatName sluit de identiteit af
                    self._switch_heat()
                elif self.cur and self.cur.heat.get("HeatNumber") == self.heat_info.get("HeatNumber"):
                    self.cur.heat[f[1]] = f[2]
                self.changed()
            elif sec == "Layout" and len(f) >= 2:
                self.layout = f[1]; self.changed()
            elif sec.startswith("Record") and len(f) >= 3:
                self.records.setdefault(sec, {})[f[1]] = f[2]; self.changed()
            elif sec == "Competitor" and len(f) >= 3:
                if self.cur is None:
                    self.cur = Heat(self.event, self.heat_info)
                n = to_int(f[1])
                if n is not None:
                    self.cur.lane(n).info[f[2]] = f[3] if len(f) > 3 else ""
                    self.changed()

    def _time(self, f, now):
        n = to_int(f[1], 0)
        kind = f[2]
        val = to_int(f[3], -1)
        d = dict(zip(f[2::2], f[3::2]))
        if self.cur is None:
            self.cur = Heat(self.event, self.heat_info)
        h = self.cur

        if kind == "RunningTime":
            if val < 0:
                self.clock_status = "idle"
                self.clock_rt = None
            else:
                new_race = self.prev_rt is None or self.prev_rt < 0 or val < self.prev_rt
                if new_race and h.started and h.has_times():
                    # herstart binnen dezelfde reeks (valse start): tijden wissen
                    for l in h.lanes.values():
                        l.splits.clear()
                if new_race:
                    h.t0 = None
                est = now - val / 10000
                h.t0 = est if h.t0 is None else min(h.t0, est)
                h.started = True
                self.prev_heat = None
                self.clock_status = "running"
                self.clock_rt = val
            self.prev_rt = val
            return  # klok gaat via apart event

        if kind == "Ready":
            if self.clock_status != "running":
                self.clock_status = "ready"
            return

        if kind == "ReactionTime":
            h.lane(n).reaction = val if val >= 0 else None
            self.changed()
            return

        if kind == "LaneTime":
            lane = h.lane(n)
            if val < 0:
                lane.clear_at = now
                self.changed()
                return
            lap = to_int(d.get("Lap"), 0)
            rank = to_int(d.get("Rank"), -1)
            old = lane.splits.get(lap)
            if old and old["time"] == val:
                lane.clear_at = None
                old["rank"] = rank if rank > 0 else old["rank"]
                self.changed()
                return
            manual = False
            if h.t0 is not None:
                delay = now - (h.t0 + val / 10000)
                manual = delay > SETTINGS["manual_threshold_s"]
            if lane.clear_at is not None:
                # -1 gevolgd door nieuwe waarde: latere laps vervallen
                for k in [k for k in lane.splits if k > lap]:
                    del lane.splits[k]
                lane.clear_at = None
            lane.splits[lap] = {"time": val, "rank": rank, "manual": manual,
                                "overwrittenFrom": old["time"] if old else None}
            if old and old.get("manual"):
                lane.splits[lap]["manual"] = True
            self.changed()

    def tick(self, now):
        """Openstaande -1's toepassen als er geen nieuwe waarde kwam."""
        with self.lock:
            if not self.cur:
                return
            for l in self.cur.lanes.values():
                if l.clear_at is not None and now - l.clear_at > SETTINGS["clear_grace_s"]:
                    l.splits.clear()
                    l.clear_at = None
                    self.changed()

    # ---------- JSON ----------
    @staticmethod
    def _event_json(e):
        return {"number": e.get("EventName", ""), "discipline": e.get("Discipline", ""),
                "startTime": tod(e.get("StartTime"))}

    @staticmethod
    def _heat_json(hi, disc=""):
        return {"number": hi.get("HeatNumber", ""), "name": hi.get("HeatName", ""),
                "distance": total_distance(disc, hi.get("DistanceM")), "laps": to_int(hi.get("Laps"), 0),
                "startTime": tod(hi.get("StartTime")), "description": hi.get("Description", "")}

    def _lanes_json(self, h):
        laps = h.laps
        per_lap = (h.distance / laps) if laps else 0
        # plaatsen zelf herberekenen per lap (SwimTime stuurt bij een manuele tijd de andere plaatsen niet opnieuw)
        by_lap = {}
        for l in h.lanes.values():
            for lap, s in l.splits.items():
                by_lap.setdefault(lap, []).append(s["time"])
        out = []
        for n in sorted(h.lanes):
            l = h.lanes[n]
            if not l.has_content():
                continue
            splits = []
            for lap in sorted(l.splits):
                s = l.splits[lap]
                rank = 1 + sum(1 for t in by_lap[lap] if t < s["time"])
                # 'suspect' blijft in het contract maar wordt enkel gevuld als SwimTime zelf een fout doorgeeft
                splits.append({"lap": lap, "distance": round(per_lap * lap) if per_lap else None,
                               "time": s["time"], "rank": rank, "manual": s["manual"],
                               "overwrittenFrom": s["overwrittenFrom"], "suspect": None})
            final = l.splits.get(laps) if laps else None
            i = l.info
            out.append({
                "lane": n, "firstName": i.get("FirstName", ""), "lastName": i.get("LastName", ""),
                "club": i.get("ClubName", "") or i.get("TeamName", ""), "nation": i.get("Nation", ""),
                "code": i.get("Code", ""), "relay": i.get("RelayName", ""),
                "reaction": l.reaction,
                "splits": splits,
                "final": final["time"] if final else None,
                "finalManual": bool(final and final["manual"]),
                "suspect": next((x["suspect"] for x in splits if x["suspect"]), None),
                "rank": splits[-1]["rank"] if splits else None,
                "finished": final is not None,
            })
        # totaalplaats: eerst wie het verst is (meeste laps), dan snelste tijd op die lap
        keyed = sorted((-o["splits"][-1]["lap"], o["splits"][-1]["time"], o["lane"]) for o in out if o["splits"])
        pos = {}
        for i, k in enumerate(keyed):
            prev = keyed[i - 1] if i else None
            pos[k[2]] = pos[prev[2]] if prev and prev[:2] == k[:2] else i + 1
        for o in out:
            o["rank"] = pos.get(o["lane"])
        return out

    def clock_json(self):
        with self.lock:
            return {"status": self.clock_status, "rt": self.clock_rt}

    def _current_enriched(self, h):
        if not h:
            return {"lanes": [], "issues": [], "officialDistances": []}
        e = enrich_heat({"event": self._event_json(h.event), "heat": self._heat_json(h.heat, h.event.get("Discipline", "")),
                         "lanes": self._lanes_json(h), "simulated": h.simulated})
        return {"lanes": e["lanes"], "issues": e["issues"], "officialDistances": e["officialDistances"]}

    def state_json(self, now):
        with self.lock:
            h = self.cur
            nxt = None
            if SETTINGS["hold_previous_until_start"] and h and not h.started and self.prev_heat is not None:
                # volgende startlijst is al geladen maar nog niet gestart: afgelopen reeks blijft groot staan
                nxt = {"event": self._event_json(h.event), "heat": self._heat_json(h.heat, h.event.get("Discipline", "")),
                       "lanes": [{"lane": o["lane"], "firstName": o["firstName"], "lastName": o["lastName"],
                                  "club": o["club"]} for o in self._lanes_json(h)
                                 if o["firstName"] or o["lastName"]]}
                h = self.prev_heat
            recs = []
            for k in sorted(self.records):
                r = self.records[k]
                if r.get("RecordName"):
                    recs.append({"type": r.get("RecordType", ""), "name": r.get("RecordName", ""),
                                 "time": to_int(r.get("RecordTime")),
                                 "athlete": f"{r.get('AthleteFirstName', '')} {r.get('AthleteLastName', '')}".strip(),
                                 "club": r.get("ClubName", ""), "nation": r.get("AthleteNation", "")})
            ago = now - self.last_packet if self.last_packet else None
            simulated = bool(STATE_SOURCE.get("sim"))
            meet = {"name": self.meet.get("MeetName", ""), "city": self.meet.get("City", ""),
                    "date": alge_date(self.meet.get("Date")),
                    "session": self.session.get("SessionNumber", ""),
                    "sessionName": self.session.get("SessionName", "")}
            no_heat = not h or (not h.event.get("EventName") and not any(l.has_content() for l in h.lanes.values()))
            if no_heat and self.history:
                # nog geen (volledige) reeks ontvangen, bv. na herstart: laatste gekende reeks tonen
                last = enrich_heat(dict(self.history[0]))
                return {
                    "connected": ago is not None and ago < SETTINGS["stale_after_s"], "simulated": simulated,
                    "settings": {k: SETTINGS[k] for k in PUBLIC_SETTINGS},
                    "lastPacketAgo": round(ago, 1) if ago is not None else None,
                    "meet": meet, "event": last["event"], "heat": last["heat"],
                    "layout": "RankOriented",
                    "clock": {"status": "idle", "rt": None},
                    "records": recs,
                    "lanes": last["lanes"], "issues": last["issues"],
                    "officialDistances": last["officialDistances"],
                    "showing": "previous", "next": None,
                }
            return {
                "connected": ago is not None and ago < SETTINGS["stale_after_s"], "simulated": simulated,
                "settings": {k: SETTINGS[k] for k in PUBLIC_SETTINGS},
                "lastPacketAgo": round(ago, 1) if ago is not None else None,
                "meet": meet,
                "event": self._event_json(h.event if h else self.event),
                "heat": self._heat_json(h.heat if h else self.heat_info, (h.event if h else self.event).get("Discipline", "")),
                "layout": self.layout,
                "clock": {"status": self.clock_status, "rt": self.clock_rt},
                "records": recs,
                **self._current_enriched(h),
                "showing": "previous" if nxt else "current",
                "next": nxt,
            }

    def history_json(self):
        with self.lock:
            return [enrich_heat(dict(h)) for h in self.history]


# ---------- publieke weergave ----------
PUBLIC_SPLIT_KEYS = ("lap", "distance", "time", "rank")


def public_lane(l, dist):
    """Enkel (semi-)officiele tijden; geen manueel/verdacht/correctie-info.
    Met public_all_splits: alle tussentijden (per lap, bv. elke 50 m), anders enkel officiele afstanden."""
    allsplits = SETTINGS["public_all_splits"]
    o = {k: v for k, v in l.items() if k not in ("splits", "finalManual", "suspect")}
    o["splits"] = [{k: sp.get(k) for k in PUBLIC_SPLIT_KEYS} | {"official": True, "manual": False,
                   "suspect": None, "overwrittenFrom": None}
                   for sp in l.get("splits", [])
                   if (sp.get("official") or (allsplits and sp.get("distance") != dist))]
    o["finalManual"] = False
    o["suspect"] = None
    if l.get("members"):
        o["members"] = [{k: m.get(k) for k in ("position", "firstName", "lastName", "club", "leg", "cum", "reaction")}
                        for m in l["members"]]
    return o


def public_distances(heat, official):
    if not SETTINGS["public_all_splits"]:
        return official
    dist, laps = heat.get("distance") or 0, heat.get("laps") or 0
    return [round(dist / laps * i) for i in range(1, laps)] if laps > 1 else []


def public_view(event, data):
    if event == "state":
        d = dict(data)
        dist = (data.get("heat") or {}).get("distance") or 0
        d["lanes"] = [public_lane(l, dist) for l in data.get("lanes", [])]
        d["officialDistances"] = public_distances(data.get("heat") or {}, data.get("officialDistances", []))
        d["issues"] = []
        d["settings"] = dict(data.get("settings", {}), show_manual=False, show_suspect=False)
        d["view"] = "publiek"
        return d
    if event == "history":
        return [dict(h, lanes=[public_lane(l, (h.get("heat") or {}).get("distance") or 0) for l in h.get("lanes", [])],
                     officialDistances=public_distances(h.get("heat") or {}, h.get("officialDistances", [])),
                     issues=[]) for h in data]
    return data


# ---------- SSE hub ----------
class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = {}             # queue -> view ("jury" | "publiek")

    def add(self, view):
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.clients[q] = view
        return q

    def remove(self, q):
        with self.lock:
            self.clients.pop(q, None)

    def send(self, event, data):
        msgs = {}
        with self.lock:
            for q, view in list(self.clients.items()):
                if view not in msgs:
                    msgs[view] = sse_msg(event, public_view(event, data) if view == "publiek" else data)
                try:
                    q.put_nowait(msgs[view])
                except queue.Full:
                    self.clients.pop(q, None)   # trage client: laten vallen, EventSource herconnecteert


def sse_msg(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")


DB_DIR = os.path.join(DATA, "db")
DB_EXPORT = os.path.join(BASE, "tools", "db_export.ps1")
DB_STATUS = {"lastSync": None, "file": None, "size": None, "sourceMtime": None, "error": None, "busy": False,
             "loadedAt": None, "loadedFile": None, "heats": 0}
DB_LOCK = threading.Lock()
DB_DATA = {}          # (eventNr, heatNr) -> {"start": t, "lanes": {lane: {"reaction": t, "laps": {lap: {...}}}}}
DB_SCHEDULE = []      # alle reeksen van de wedstrijd in programmavolgorde (voor de oproepkamer)
SIM_SCHEDULE = []     # programma van de simulator (zelfde vorm als DB_SCHEDULE)
STATE = None          # gezet in main(), om na een import de clients te verversen


def is_smb(path):
    """Windows-share op een niet-Windows-toestel (kastje): UNC-pad (dubbele backslash), //pc/share/... of smb://pc/share/..."""
    return os.name != "nt" and bool(path) and (path.startswith(("\\\\", "//", "smb://")))


def smb_split(path):
    p = path[6:] if path.startswith("smb://") else path.lstrip("\\/")
    parts = [x for x in re.split(r"[\\/]+", p) if x]
    if len(parts) < 3:
        raise OSError(f"ongeldig share-pad {path} (verwacht \\\\pc\\share\\bestand.mdb)")
    return parts[0], parts[1], "\\".join(parts[2:])


def smbclient(path, command, timeout=120):
    """smbclient uitvoeren met een tijdelijk wachtwoordbestand (niet zichtbaar in de proceslijst). Enkel lezen."""
    if not shutil.which("smbclient"):
        raise OSError("smbclient is niet geïnstalleerd (pakket smbclient)")
    srv, share, rel = smb_split(path)
    fd, auth = tempfile.mkstemp(prefix="smb-", dir=DATA)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"username = {SETTINGS.get('db_smb_user') or 'guest'}\n")
            f.write(f"password = {SETTINGS.get('db_smb_password') or ''}\n")
            if SETTINGS.get("db_smb_domain"):
                f.write(f"domain = {SETTINGS['db_smb_domain']}\n")
        os.chmod(auth, 0o600)
        args = ["smbclient", f"//{srv}/{share}", "-A", auth, "-c", command(rel)]
        if not SETTINGS.get("db_smb_user"):
            args.insert(2, "-N")
        r = subprocess.run(args, capture_output=True, timeout=timeout)
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        if r.returncode != 0 or "NT_STATUS_" in out:
            m = re.search(r"NT_STATUS_[A-Z_]+", out)
            hint = {"NT_STATUS_LOGON_FAILURE": "gebruiker of wachtwoord fout",
                    "NT_STATUS_OBJECT_NAME_NOT_FOUND": "bestand niet gevonden",
                    "NT_STATUS_OBJECT_PATH_NOT_FOUND": "map niet gevonden",
                    "NT_STATUS_BAD_NETWORK_NAME": "share bestaat niet",
                    "NT_STATUS_ACCESS_DENIED": "geen toegang",
                    "NT_STATUS_HOST_UNREACHABLE": "pc onbereikbaar",
                    "NT_STATUS_IO_TIMEOUT": "pc antwoordt niet"}.get(m.group(0) if m else "", "")
            raise OSError(f"share {srv}/{share}: " + (f"{hint} ({m.group(0)})" if hint else (m.group(0) if m else out.strip()[-200:])))
        return out
    finally:
        try:
            os.remove(auth)
        except OSError:
            pass


def db_src_mtime(path):
    """Wijzigingstijd van de bron (lokaal pad of share), enkel om te vergelijken."""
    if is_smb(path):
        out = smbclient(path, lambda rel: f'allinfo "{rel}"', timeout=20)
        m = re.search(r"write_time:\s*(.+)", out)
        return m.group(1).strip() if m else out.strip()[-80:]
    if path and not os.path.isabs(path):
        path = os.path.normpath(os.path.join(BASE, path))
    return os.path.getmtime(path)


def db_copy(path):
    """Eén kopie van de SwimTime-database naar db/. De bron wordt enkel lezend geopend en anderen
    mogen blijven lezen/schrijven (geen Access-locks, geen .ldb). Geeft het pad van de kopie terug.
    Een relatief pad (bv. ..\2026_PK.mdb) geldt t.o.v. de map van de toepassing."""
    smb = is_smb(path)
    if path and not smb and not os.path.isabs(path):
        path = os.path.normpath(os.path.join(BASE, path))
    if not smb and (not path or not os.path.isfile(path)):
        raise OSError(f"bestand niet gevonden: {path} (is de netwerkschijf verbonden?)")
    os.makedirs(DB_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base, ext = os.path.splitext(re.split(r"[\\/]", path)[-1])
    dst = os.path.join(DB_DIR, f"{base}_{stamp}{ext}")
    if smb:            # kopie ophalen van de Windows-share: enkel lezen, geen koppeling, geen .ldb
        src_mtime = None
        smbclient(path, lambda rel: f'get "{rel}" "{dst}.part"')
    else:
        src_mtime = os.path.getmtime(path)
        with open(path, "rb") as fsrc, open(dst + ".part", "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, 1024 * 1024)
    os.replace(dst + ".part", dst)
    olds = sorted(f for f in os.listdir(DB_DIR) if f.startswith(base + "_") and f.endswith(ext))
    for f in olds[:-5]:               # enkel de laatste 5 kopieen bewaren
        try:
            os.remove(os.path.join(DB_DIR, f))
        except OSError:
            pass
    DB_STATUS.update(lastSync=datetime.now().strftime("%H:%M:%S"), file=os.path.basename(dst),
                     size=os.path.getsize(dst),
                     sourceMtime=datetime.fromtimestamp(src_mtime).strftime("%H:%M:%S") if src_mtime else None)
    return dst


def db_parse(rows):
    """Ruwe Times-rijen -> per reeks start, reactie en per lap paneel/peer/manueel (relatief t.o.v. de start)."""
    by_heat = {}
    for ev, ht, lane, lap, t, ch, side, inv in rows:
        by_heat.setdefault((str(ev), str(ht)), []).append((int(lane), int(lap), int(t), ch, side, inv))
    out = {}
    for key, rs in by_heat.items():
        first_lap = min((t for _, lap, t, ch, _, _ in rs if lap >= 1 and ch in ("T", "2", "C")), default=None)
        starts = [t for _, _, t, ch, _, _ in rs if ch == "E" and (first_lap is None or t < first_lap)]
        if not starts:
            continue
        start = max(starts)             # laatste start voor de eerste aantikking (valse start = herstart)
        lanes = {}
        for lane, lap, t, ch, side, inv in rs:
            if t < start:
                continue
            L = lanes.setdefault(lane, {"reaction": None, "laps": {}, "blocks": {}})
            rel = t - start
            if ch == "B" and inv == "N" and lap == 0 and L["reaction"] is None and rel < 30000:
                L["reaction"] = rel
            if ch == "B" and inv == "N" and lap >= 1:
                L["blocks"].setdefault(lap, []).append(rel)   # aflossing: volgende zwemmer verlaat het blok
            if lap < 1:
                continue
            P = L["laps"].setdefault(lap, {"panel": None, "peer": None, "manual": None})
            if ch == "T" and inv == "N" and P["panel"] is None:
                P["panel"] = rel
            elif ch == "2" and inv == "N" and P["peer"] is None:
                P["peer"] = rel
            elif ch == "C":
                P["manual"] = rel
        out[key] = {"start": start, "lanes": lanes}
    return out


def db_load(local_file):
    """Leest de LOKALE kopie via tools/db_export.ps1 (Access-driver, read-only)."""
    sess = 0
    if STATE is not None:
        sess = to_int(STATE.session.get("SessionNumber"), 0) or 0
    if os.name != "nt":                       # kastje: mdbtools (zelfde uitvoer als db_export.ps1)
        sys.path.insert(0, os.path.join(BASE, "tools"))
        import db_mdbtools
        if not shutil.which("mdb-export"):
            raise OSError("mdbtools is niet geïnstalleerd (pakket mdbtools)")
        data = db_mdbtools.export(local_file, sess)
    else:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", DB_EXPORT,
                            "-File", local_file, "-SessionNumber", str(sess)],
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            raise OSError("database lezen mislukt: " + r.stderr.decode("utf-8", "replace").strip()[:300])
        data = json.loads(r.stdout.decode("utf-8-sig"))
    if data.get("error"):
        raise OSError(data["error"])
    parsed = db_parse(data.get("rows") or [])
    for ev, ht, lane, team, teamnr, pos, fn, ln, club in data.get("relays") or []:
        H = parsed.setdefault((str(ev), str(ht)), {"start": None, "lanes": {}})
        L = H["lanes"].setdefault(int(lane), {"reaction": None, "laps": {}, "blocks": {}})
        L["team"] = team
        L.setdefault("members", {})[int(pos)] = {"firstName": fn, "lastName": ln, "club": club}
    DB_DATA.clear()
    DB_DATA.update(parsed)
    sched, idx = [], {}
    for (sess, date, ev, disc, dist, relay, ht, hname, htime, finished, lane, entry, lstatus,
         fn, ln, club, team) in data.get("schedule") or []:
        key = (str(ev), str(ht))
        if key not in idx:
            idx[key] = len(sched)
            sched.append({"session": sess, "date": date, "event": {"number": str(ev), "discipline": disc},
                          "heat": {"number": str(ht), "name": hname, "distance": dist, "startTime": htime},
                          "relay": (relay or 1) > 1, "finished": bool(finished), "lanes": [], "lanesKnown": False})
        if lane is not None and not isinstance(lane, str):
            sched[idx[key]]["lanesKnown"] = True
        if fn or ln or team:
            sched[idx[key]]["lanes"].append({"lane": lane, "firstName": fn, "lastName": ln, "club": club or (team or "").split(" ")[0],
                                             "team": team, "entryTime": entry, "status": lstatus})
    DB_SCHEDULE[:] = sched
    DB_STATUS.update(loadedAt=datetime.now().strftime("%H:%M:%S"), loadedFile=os.path.basename(local_file),
                     heats=len(parsed))
    if STATE is not None:
        with STATE.lock:
            STATE.history_version += 1
            STATE.changed()


def db_direct():
    """Rechtstreeks lezen (zonder kopie): enkel op het kastje – mdbtools opent alleen-lezen, de share is ro gekoppeld.
    Op Windows altijd via een kopie: de Access-driver maakt anders een .ldb-bestand naast de database."""
    return SETTINGS.get("db_access_mode") == "rechtstreeks" and os.name != "nt"


def db_read_direct(path):
    if is_smb(path):
        raise OSError("rechtstreeks lezen kan enkel van een gekoppelde netwerkschijf: koppel de share in "
                      "Netwerkschijven en kies daar het bestand")
    if path and not os.path.isabs(path):
        path = os.path.normpath(os.path.join(BASE, path))
    if not path or not os.path.isfile(path):
        raise OSError(f"bestand niet gevonden: {path} (is de netwerkschijf gekoppeld?)")
    try:
        db_load(path)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        time.sleep(2)                 # SwimTime schreef mogelijk net: één nieuwe poging
        db_load(path)
    DB_STATUS.update(lastSync=datetime.now().strftime("%H:%M:%S"), file=os.path.basename(path) + " (rechtstreeks)",
                     size=os.path.getsize(path),
                     sourceMtime=datetime.fromtimestamp(os.path.getmtime(path)).strftime("%H:%M:%S"))


def db_refresh(path=None, reason="manueel"):
    """Kopie nemen + inlezen. Een kopie die niet leesbaar is (SwimTime schreef net) wordt weggegooid.
    Kastje met "rechtstreeks": het bestand op de alleen-lezen gekoppelde share zelf inlezen (nooit iets verwijderen)."""
    path = path or SETTINGS["db_source_path"]
    with DB_LOCK:
        if DB_STATUS["busy"]:
            return False, "er loopt al een synchronisatie"
        DB_STATUS["busy"] = True
    try:
        if db_direct():
            try:
                db_read_direct(path)
            except (ValueError, subprocess.TimeoutExpired) as e:
                raise OSError(f"database niet leesbaar (SwimTime schreef mogelijk net): {e}")
            DB_STATUS["error"] = None
            print(f"database rechtstreeks gelezen ({reason}): {DB_STATUS['heats']} reeksen")
            return True, None
        dst = db_copy(path)
        try:
            db_load(dst)
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            try:
                os.remove(dst)
            except OSError:
                pass
            raise OSError(f"kopie niet leesbaar (SwimTime schreef mogelijk net): {e}")
        DB_STATUS["error"] = None
        print(f"database gesynchroniseerd ({reason}): {DB_STATUS['heats']} reeksen")
        return True, None
    except OSError as e:
        DB_STATUS["error"] = str(e)
        print(f"database-synchronisatie mislukt ({reason}): {e}")
        return False, str(e)
    finally:
        DB_STATUS["busy"] = False


def db_after_heat():
    """Na het laden van een nieuwe startlijst: na een korte wachttijd één kopie; bij mislukking één nieuwe poging."""
    if not SETTINGS["db_auto_after_heat"] or STATE is None:
        return

    def run():
        ok, _ = db_refresh(reason="na reeks")
        if not ok:
            time.sleep(15)
            db_refresh(reason="na reeks, 2e poging")

    t = threading.Timer(float(SETTINGS["db_after_heat_delay_s"]), run)
    t.daemon = True
    t.start()


def db_interval_loop():
    """Live database: elke X s (min. 1) kijken of het bronbestand gewijzigd is (één stat-aanvraag),
    en enkel dan een kopie nemen. 0 = uit."""
    last_mtime = None
    while True:
        iv = float(SETTINGS["db_interval_s"] or 0)
        if iv <= 0:
            last_mtime = None
            time.sleep(2)            # uit: af en toe kijken of het vinkje aan ging
            continue
        time.sleep(max(iv, 1))
        src = SETTINGS["db_source_path"]
        try:
            m = db_src_mtime(src)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if m == last_mtime:
            continue
        # wachten tot het bestand 1 s niet meer verandert (SwimTime schrijft in meerdere stappen), max 5 s
        deadline = time.time() + 5
        while time.time() < deadline:
            time.sleep(1)
            try:
                m2 = db_src_mtime(src)
            except (OSError, subprocess.TimeoutExpired):
                break
            if m2 == m:
                break
            m = m2
        ok, _ = db_refresh(reason="live")
        if ok:
            last_mtime = m


def db_load_latest_local():
    """Bij opstart: de nieuwste lokale kopie inlezen (geen netwerkverkeer); bij "rechtstreeks" de bron zelf."""
    if db_direct():
        time.sleep(15)       # netwerkschijf koppelt kort na de start
        db_refresh(reason="opstart")
        return
    try:
        files = sorted((os.path.join(DB_DIR, f) for f in os.listdir(DB_DIR) if f.lower().endswith((".mdb", ".accdb"))),
                       key=os.path.getmtime)
    except OSError:
        return
    if files:
        time.sleep(3)    # even wachten tot het sessienummer uit de broadcast binnen is
        try:
            db_load(files[-1])
            print(f"lokale database-kopie ingelezen: {os.path.basename(files[-1])} ({DB_STATUS['heats']} reeksen)")
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            print("lokale database-kopie niet leesbaar:", e)


PROG_SCHEDULE = []    # volledig programma uit een Lenex-export van Meet Manager (ook toekomstige reeksen)
PROG_STATUS = {"file": None, "loadedAt": None, "stats": None, "error": None}
PROG_FILE = os.path.join(DATA, "db", "programma.json")


def prog_load():
    try:
        d = json.load(open(PROG_FILE, encoding="utf-8"))
        PROG_SCHEDULE[:] = d.get("schedule") or []
        PROG_STATUS.update(d.get("status") or {})
        print(f"programma geladen: {PROG_STATUS.get('file')} ({len(PROG_SCHEDULE)} reeksen)")
    except (OSError, ValueError):
        pass


DAYS_NL = ["ma", "di", "wo", "do", "vr", "za", "zo"]


def day_part(t):
    """Dagdeel uit een tijd 'HH:MM' (voormiddag / namiddag / avond)."""
    try:
        h, m = (int(x) for x in str(t).split(":")[:2])
    except ValueError:
        return ""
    mins = h * 60 + m
    return "voormiddag" if mins < 12 * 60 else ("namiddag" if mins < 17 * 60 + 30 else "avond")


def label_schedule(sched):
    """Per reeks een label 'za · voormiddag' (wedstrijden van een halve dag tot meerdere dagen met meerdere dagdelen)."""
    days = sorted({h.get("date") or "" for h in sched})
    multi_day = len([d for d in days if d]) > 1
    for h in sched:
        part = day_part((h.get("heat") or {}).get("startTime"))
        d = h.get("date") or ""
        try:
            wd = DAYS_NL[datetime.strptime(d, "%Y-%m-%d").weekday()] if d else ""
        except ValueError:
            wd = ""
        h["dayPart"] = part
        h["dayLabel"] = " · ".join(x for x in ((wd + " " + d[8:10] + "/" + d[5:7]) if (wd and multi_day) else "", part) if x)
    return sched


def prog_import(data, filename):
    """Programma inlezen: Lenex (.lxf/.lef/.xml) of een Splash Meet Manager-backup (.smb)."""
    name = (filename or "").lower()
    sys.path.insert(0, os.path.join(BASE, "tools"))
    if name.endswith(".smb") or (data[:2] == b"PK" and b".gbin" in data[:65536]):
        import mm_backup
        sched, stats = mm_backup.parse(data)
        stats = dict(stats, format="Meet Manager-backup")
    else:
        import lenex
        sched, stats = lenex.parse(data, filename)
        stats = dict(stats or {}, format="Lenex")
    if not sched:
        raise ValueError("geen reeksen gevonden in dit bestand")
    sched = label_schedule(sched)
    PROG_SCHEDULE[:] = sched
    PROG_STATUS.update(file=os.path.basename(filename or "programma"), loadedAt=datetime.now().strftime("%d/%m %H:%M"),
                       stats=stats, error=None)
    os.makedirs(os.path.dirname(PROG_FILE), exist_ok=True)
    tmp = PROG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"schedule": sched, "status": PROG_STATUS}, f, ensure_ascii=False)
    os.replace(tmp, PROG_FILE)
    return stats


def prog_clear():
    PROG_SCHEDULE.clear()
    PROG_STATUS.update(file=None, loadedAt=None, stats=None, error=None)
    try:
        os.remove(PROG_FILE)
    except OSError:
        pass


def merged_schedule():
    """Programma uit Lenex, aangevuld met de actuele baanindeling en 'afgewerkt' uit de SwimTime-database."""
    if not PROG_SCHEDULE:
        return DB_SCHEDULE
    dbi = {(h["event"]["number"], h["heat"]["number"]): h for h in DB_SCHEDULE}
    done = set()
    if STATE is not None:
        for it in STATE.history:
            m = re.search(r"(\d+)\s*$", (it.get("heat") or {}).get("name") or "")
            done.add((str((it.get("event") or {}).get("number") or ""), m.group(1) if m else str((it.get("heat") or {}).get("number"))))
    out = []
    for h in PROG_SCHEDULE:
        k = (h["event"]["number"], h["heat"]["number"])
        it = dict(h)
        d = dbi.get(k)
        if d:
            if d.get("lanesKnown") and d.get("lanes"):
                it["lanes"], it["lanesKnown"] = d["lanes"], True
            it["finished"] = bool(d.get("finished"))
        if k in done:
            it["finished"] = True
        out.append(it)
    return out


def callroom_json(state_now):
    """Volgende N reeksen na de huidige live reeks, uit het programma van de databasekopie."""
    n = max(1, int(SETTINGS["callroom_heats"]))
    ev = str((state_now.get("event") or {}).get("number") or "")
    m = re.search(r"(\d+)\s*$", (state_now.get("heat") or {}).get("name") or "")
    ht = m.group(1) if m else str((state_now.get("heat") or {}).get("number") or "")
    sim = bool(STATE_SOURCE.get("sim"))
    sched = SIM_SCHEDULE if sim else merged_schedule()
    pos = next((i for i, h in enumerate(sched) if h["event"]["number"] == ev and h["heat"]["number"] == ht), None)
    if pos is None:
        today = datetime.now().strftime("%Y-%m-%d")
        pos = next((i - 1 for i, h in enumerate(sched) if not h["finished"] and h["date"] >= today), len(sched))
    # volgende, nog niet afgewerkte reeksen; ook zonder startlijst (lanes leeg / lanesKnown false)
    # +1: de eerste reeks na de live reeks staat al aan de startblok, de oproepkamer zijn de n reeksen daarna
    upcoming = [h for h in sched[pos + 1:] if not h["finished"]][:n + 1]
    live = sched[pos] if pos is not None and 0 <= pos < len(sched) else None
    return {"upcoming": upcoming, "count": n, "matched": pos is not None and 0 <= pos < len(sched), "simulated": sim,
            "liveLabel": (live or {}).get("dayLabel", ""),
            "live": {"event": ev, "heat": ht},          # voor welke live reeks deze lijst berekend is
            "db": {"loadedAt": "simulator" if sim else (PROG_STATUS["loadedAt"] if PROG_SCHEDULE else DB_STATUS["loadedAt"]),
                   "loadedFile": "simulatie" if sim else (PROG_STATUS["file"] if PROG_SCHEDULE else DB_STATUS["loadedFile"]),
                   "heats": len(sched)}}


def db_status_json():
    return {k: DB_STATUS[k] for k in ("lastSync", "sourceMtime", "error", "busy", "loadedAt", "loadedFile", "heats")} | {
        "source": SETTINGS["db_source_path"], "autoAfterHeat": SETTINGS["db_auto_after_heat"],
        "intervalS": SETTINGS["db_interval_s"], "liveIntervalS": SETTINGS["db_live_interval_s"],
        "live": float(SETTINGS["db_interval_s"] or 0) > 0}


class RelayPusher:
    """Zend-modus: stuurt de verwerkte stand met HTTPS-POST naar de publieke server (enkel uitgaand)."""
    def __init__(self, url, token):
        u = urllib.parse.urlsplit(url)
        self.https = u.scheme == "https"
        self.host, self.port = u.hostname, u.port or (443 if self.https else 80)
        self.path = (u.path.rstrip("/") or "") + "/ingest"
        self.token = token
        self.latest, self.lock, self.ev = {}, threading.Lock(), threading.Event()
        self.ok, self.error, self.last_ok, self.sent = False, None, None, 0
        self.stopped = False
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, kind, data):
        with self.lock:
            self.latest[kind] = data
        self.ev.set()

    def status(self):
        return {"url": f"{'https' if self.https else 'http'}://{self.host}:{self.port}", "ok": self.ok,
                "error": self.error, "lastOk": self.last_ok, "sent": self.sent}

    def _run(self):
        conn, backoff = None, 1.0
        while not self.stopped:
            self.ev.wait(5)
            self.ev.clear()
            if self.stopped:
                break
            with self.lock:
                batch, self.latest = self.latest, {}
            if not batch:
                continue
            body = json.dumps({"items": [{"t": k, "d": v} for k, v in batch.items()]},
                              ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            fresh = conn is None
            try:
                if conn is None:
                    conn = (http.client.HTTPSConnection if self.https else http.client.HTTPConnection)(
                        self.host, self.port, timeout=8)
                conn.request("POST", self.path, body=body,
                             headers={"Content-Type": "application/json", "X-Ingest-Token": self.token,
                                      "User-Agent": "KHZS-livetiming/1.0", "Content-Length": str(len(body))})
                r = conn.getresponse()
                r.read()
                if r.status != 200:
                    raise OSError(f"HTTP {r.status}")
                self.ok, self.error, self.last_ok, self.sent, backoff = True, None, time.time(), self.sent + 1, 1.0
            except (OSError, http.client.HTTPException) as e:
                self.ok, self.error = False, str(e)
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
                with self.lock:           # niets verliezen: opnieuw proberen met de nieuwste stand
                    for k, v in batch.items():
                        self.latest.setdefault(k, v)
                if not fresh:             # keep-alive door proxy gesloten: meteen opnieuw met een nieuwe verbinding
                    self.ev.set()
                    continue
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
                self.ev.set()


class RelayStore:
    """Relay-modus: houdt de laatst ontvangen stand bij (ook op schijf) en gedraagt zich als State voor de handler."""
    STALE_S = 15.0

    def __init__(self, cache_path=None):
        self.lock = threading.RLock()
        self.cache_path = cache_path
        self.dirty = False
        self.state, self.history, self.clock = None, [], {"status": "idle", "rt": None}
        self.callroom, self.db, self.last_ingest = None, None, 0.0
        self._load()
        if cache_path:
            threading.Thread(target=self._saver, daemon=True).start()

    def _load(self):
        if not self.cache_path or not os.path.exists(self.cache_path):
            return
        try:
            d = json.load(open(self.cache_path, encoding="utf-8"))
            self.state, self.history = d.get("state"), d.get("history") or []
            self.callroom, self.db = d.get("callroom"), d.get("db")
            self.last_ingest = float(d.get("last_ingest") or 0)
            self.clock = {"status": "idle", "rt": None}
            print(f"laatste stand geladen uit {self.cache_path} "
                  f"({len(self.history)} reeksen, ontvangen {time.strftime('%d/%m %H:%M', time.localtime(self.last_ingest))})")
        except (OSError, ValueError) as e:
            print("cache niet leesbaar:", e)

    def clear(self):
        """Beheer: laatst ontvangen stand vergeten (ook op schijf)."""
        with self.lock:
            self.state, self.history, self.clock = None, [], {"status": "idle", "rt": None}
            self.callroom, self.db, self.last_ingest = None, None, 0.0
            self.dirty = False
        if self.cache_path and os.path.exists(self.cache_path):
            os.remove(self.cache_path)

    def _saver(self):
        while True:
            time.sleep(5)
            with self.lock:
                if not self.dirty:
                    continue
                self.dirty = False
                data = {"state": self.state, "history": self.history, "callroom": self.callroom,
                        "db": self.db, "last_ingest": self.last_ingest}
            tmp = self.cache_path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp, self.cache_path)
            except OSError as e:
                print("cache schrijven mislukt:", e)

    def stale(self):
        return not self.last_ingest or time.time() - self.last_ingest > self.STALE_S

    def ingest(self, kind, data):
        with self.lock:
            self.last_ingest = time.time()
            if kind != "clock":
                self.dirty = True
            if kind == "state":
                self.state = data
            elif kind == "history":
                self.history = data if isinstance(data, list) else []
            elif kind == "clock":
                self.clock = data
            elif kind == "callroom":
                self.callroom = data
            elif kind == "db":
                self.db = data

    def state_json(self, now=None):
        with self.lock:
            ago = time.time() - self.last_ingest if self.last_ingest else None
            d = dict(self.state) if self.state else {"event": {}, "heat": {}, "lanes": [], "issues": [],
                                                      "officialDistances": [], "meet": {}, "layout": "",
                                                      "clock": self.clock, "records": [],
                                                      "settings": {k: SETTINGS[k] for k in PUBLIC_SETTINGS},
                                                      "showing": "current", "next": None}
            d["relay"] = True
            d["relayAgo"] = round(ago, 1) if ago is not None else None
            if ago is None or ago > self.STALE_S:
                d["connected"] = False
                d["lastPacketAgo"] = d.get("relayAgo")
                d["clock"] = {"status": "idle", "rt": None}     # geen data meer: klok niet laten doorlopen
            return d

    def history_json(self):
        with self.lock:
            return list(self.history)

    def clock_json(self):
        with self.lock:
            return {"status": "idle", "rt": None} if self.stale() else dict(self.clock)


def relay_watchdog(relay, hub):
    """Open pagina's op de hoogte houden als de laptop stopt met zenden (en de SSE-verbinding levend houden)."""
    was_stale = None
    while True:
        time.sleep(3)
        st = relay.stale()
        if st or st != was_stale:
            hub.send("state", relay.state_json())
            if st:
                hub.send("clock", relay.clock_json())
        was_stale = st


# ---------------------------------------------------------------- appliance (Raspberry Pi)
AGENT_URL = os.environ.get("KHZS_AGENT", "").rstrip("/")        # bv. http://127.0.0.1:8091 (enkel op de Pi)
AGENT_KEY_FILE = os.environ.get("KHZS_AGENT_KEY_FILE", "/var/lib/khzs/agent.key")


def agent_key():
    try:
        return open(AGENT_KEY_FILE).read().strip()
    except OSError:
        return ""
AGENT_STATUS = {"ap": False, "apIp": "10.42.0.1", "at": 0.0}
CAPTIVE_PATHS = ("/generate_204", "/gen_204", "/hotspot-detect.html", "/library/test/success.html", "/ncsi.txt",
                 "/connecttest.txt", "/canonical.html", "/success.txt", "/redirect", "/mobile/status.php")


def agent_call(method, path, body=None, headers=None, timeout=30):
    """Stuurt een verzoek door naar de systeemdienst (localhost). Geeft (status, content-type, bytes)."""
    if not AGENT_URL:
        return 404, "application/json", b'{"error":"geen systeemdienst (enkel op de Pi)"}'
    u = urllib.parse.urlsplit(AGENT_URL)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    headers = dict(headers or {}, **{"X-Agent-Key": agent_key()})
    try:
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        return r.status, r.getheader("Content-Type", "application/json"), r.read()
    except OSError as e:
        return 502, "application/json", json.dumps({"error": f"systeemdienst niet bereikbaar: {e}"}).encode()
    finally:
        conn.close()


def agent_poll():
    """Houdt bij of de Pi zijn eigen hotspot aanbiedt (voor de captive-portal-omleiding)."""
    while True:
        st, _, body = agent_call("GET", "/status", timeout=5)
        if st == 200:
            try:
                d = json.loads(body)
                AGENT_STATUS.update(ap=bool(d.get("hotspot", {}).get("active")),
                                    apIp=d.get("hotspot", {}).get("ip") or "10.42.0.1", at=time.time())
            except ValueError:
                pass
        time.sleep(5)


LOGIN_FAIL_LOCK = threading.Lock()


def session_token(password):
    return hmac.new(password.encode("utf-8"), b"lt-session-v1", hashlib.sha256).hexdigest()[:40]


LOGIN_HTML = """<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Aanmelden – Live timing</title>
<style>:root{color-scheme:dark;--bg:#07131f;--card:#0e2233;--border:#23445e;--text:#e8f1f8;--muted:#9bb2c4;--accent:#38bdf8;--danger:#ff5d5d}
@media (prefers-color-scheme:light){:root{color-scheme:light;--bg:#eef3f7;--card:#fff;--border:#c5d4e0;--text:#0b1f30;--muted:#4b6479;--accent:#0369a1;--danger:#c81e1e}}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);color:var(--text);font:16px system-ui,Segoe UI,Roboto,sans-serif}
form{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:28px 26px;width:min(92vw,360px);box-shadow:0 10px 40px rgba(0,0,0,.25)}
h1{font-size:20px;margin:0 0 4px}p{margin:0 0 18px;color:var(--muted);font-size:14px}
input{width:100%;box-sizing:border-box;font-size:18px;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--text)}
button{margin-top:14px;width:100%;font-size:16px;font-weight:700;padding:11px;border:0;border-radius:8px;background:var(--accent);color:#06121c;cursor:pointer}
.err{color:var(--danger);font-weight:600;margin:10px 0 0;font-size:14px}a{color:var(--muted);font-size:13px;display:block;text-align:center;margin-top:16px}</style></head>
<body><form method="post" action="/login"><h1>__TITLE__</h1><p>Deze pagina is enkel voor jury en organisatie.</p>
<input type="password" name="password" placeholder="Wachtwoord" autofocus autocomplete="current-password" required>
<input type="hidden" name="next" value="__NEXT__"><button type="submit">Aanmelden</button>__ERR__
<a href="/">← naar de publieke uitslagen</a></form></body></html>"""


class Stats:
    """Kijkersstatistieken (vooral voor de publieke server): live per pagina, piek en unieke bezoekers per dag,
    verloop per minuut (24 u). Bezoekers worden geteld met een hash met dagelijks wisselend zout: geen IP-adressen."""
    PAGES = ("publiek", "jury", "callroom")

    def __init__(self, path=None):
        self.lock = threading.Lock()
        self.path = path
        self.live = {}                       # verbinding -> (pagina, bezoeker)
        self.day = time.strftime("%Y-%m-%d")
        self.salt = _secrets.token_hex(16)
        self.unique = {p: set() for p in self.PAGES}
        self.views = {p: 0 for p in self.PAGES}
        self.peak = {"value": 0, "at": None}
        self.timeline = []                   # [epoch-minuut, publiek, jury, callroom]
        self.days = {}                       # datum -> {unique, peak, views}
        self._load()
        threading.Thread(target=self._sampler, daemon=True).start()

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            d = json.load(open(self.path, encoding="utf-8"))
            self.timeline = [r for r in d.get("timeline", []) if r[0] > time.time() - 86400]
            self.days = d.get("days", {})
            t = self.days.get(self.day)
            if t:
                self.peak = t.get("peakInfo") or self.peak
                self.views.update(t.get("views") or {})
        except (OSError, ValueError):
            pass

    def _save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"timeline": self.timeline, "days": self.days}, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            pass

    def visitor(self, handler):
        ip = handler.headers.get("Cf-Connecting-Ip") or handler.client_address[0]
        ua = handler.headers.get("User-Agent", "")
        return hashlib.sha256((self.salt + ip + "|" + ua).encode()).hexdigest()[:16]

    def _roll(self):
        today = time.strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.salt = today, _secrets.token_hex(16)
            self.unique = {p: set() for p in self.PAGES}
            self.views = {p: 0 for p in self.PAGES}
            self.peak = {"value": 0, "at": None}

    def open(self, key, page, vid):
        with self.lock:
            self._roll()
            page = page if page in self.PAGES else "publiek"
            self.live[key] = (page, vid)
            self.unique[page].add(vid)
            self.views[page] += 1

    def close(self, key):
        with self.lock:
            self.live.pop(key, None)

    def now(self):
        with self.lock:
            c = {p: 0 for p in self.PAGES}
            for page, _ in self.live.values():
                c[page] += 1
            c["total"] = sum(c[p] for p in self.PAGES)
            c["people"] = len({v for _, v in self.live.values()})
            return c

    def _sampler(self):
        last_save = time.time()
        while True:
            time.sleep(60 - time.time() % 60)
            n = self.now()
            with self.lock:
                self._roll()
                self.timeline.append([int(time.time() // 60 * 60)] + [n[p] for p in self.PAGES])
                cut = time.time() - 86400
                while self.timeline and self.timeline[0][0] < cut:
                    self.timeline.pop(0)
                if n["total"] > self.peak["value"]:
                    self.peak = {"value": n["total"], "at": time.strftime("%H:%M")}
                self.days[self.day] = {"unique": len(set().union(*self.unique.values())),
                                       "uniquePages": {p: len(v) for p, v in self.unique.items()},
                                       "peak": self.peak["value"], "peakInfo": self.peak, "views": dict(self.views)}
                for d in sorted(self.days)[:-31]:
                    self.days.pop(d, None)
            if time.time() - last_save > 300:
                self._save()
                last_save = time.time()

    def report(self):
        n = self.now()
        with self.lock:
            uniq = len(set().union(*self.unique.values()))
            yday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
            return {"now": n, "peakToday": self.peak, "uniqueToday": uniq,
                    "uniquePagesToday": {p: len(v) for p, v in self.unique.items()},
                    "viewsToday": dict(self.views), "yesterday": self.days.get(yday),
                    "days": {d: {k: v for k, v in x.items() if k != "peakInfo"} for d, x in sorted(self.days.items())[-14:]},
                    "timeline": self.timeline[-1440:], "pages": list(self.PAGES)}


STATS = None

# ---------- infoscherm (geen wedstrijd, volgende wedstrijd, pauze, …): lokaal en op de publieke server ----------
INFO_MODES = ("off", "auto", "geen", "volgende", "welkom", "inzwemmen", "pauze", "prijsuitreiking", "einde", "bericht",
              "presentatie")
IS_RELAY = False
INFO = {"mode": "off"}
INFO_FILE = None


def info_load(path):
    global INFO_FILE
    INFO_FILE = path
    try:
        with open(path, encoding="utf-8") as f:
            INFO.clear()
            INFO.update(json.load(f))
    except (OSError, ValueError):
        pass


def info_set(d, source="lokaal"):
    """Valideren en opslaan; geeft het nieuwe infoscherm terug.
    mode "auto": volgens de agenda (volgende wedstrijd x dagen vooraf, welkom op de dag zelf, anders geen wedstrijd)."""
    if not isinstance(d, dict) or d.get("mode", "off") not in INFO_MODES:
        raise ValueError("ongeldige modus")
    new = {"mode": d.get("mode", "off"), "updatedAt": int(time.time()), "source": source}
    # aftellen: tot een uur (until) of een duur in minuten vanaf nu (untilAt), bv. 15 min pauze
    try:
        dur = float(d.get("duration") or 0)
    except (TypeError, ValueError):
        raise ValueError("ongeldige duur")
    if 0 < dur <= 24 * 60:
        new["duration"] = dur
        new["untilAt"] = int(time.time() + dur * 60)
    elif d.get("untilAt") and not d.get("until"):
        new["untilAt"] = int(d["untilAt"])                  # ongewijzigd doorgegeven (bv. naar de cloud)
    sched = []
    for m in (d.get("schedule") or [])[:30]:
        if not isinstance(m, dict):
            continue
        date = str(m.get("date") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            continue
        start = str(m.get("start") or "").strip()
        sched.append({"name": str(m.get("name") or "").strip()[:140], "date": date,
                      "start": start if re.fullmatch(r"\d{1,2}:\d{2}", start) else "",
                      "place": str(m.get("place") or "").strip()[:120]})
    sched.sort(key=lambda m: (m["date"], m["start"]))
    new["schedule"] = sched
    try:
        new["daysBefore"] = max(0, min(90, int(d.get("daysBefore", 7))))
    except (TypeError, ValueError):
        new["daysBefore"] = 7
    for k, n in (("kicker", 60), ("footer", 120), ("title", 140), ("text", 400), ("meet", 140), ("date", 20), ("place", 120),
                 ("until", 20), ("ticker", 1500)):
        v = str(d.get(k) or "").strip()
        if v:
            new[k] = v[:n]
    new["autoHide"] = bool(d.get("autoHide", True))
    new["allowClose"] = bool(d.get("allowClose", True))
    pages = d.get("pages") or {}
    new["pages"] = {"publiek": bool(pages.get("publiek", True)), "callroom": bool(pages.get("callroom", True))}
    # presentatie: gekozen dia's en hoe lang elke dia blijft staan (ook bewaard bij een andere modus, voor het beheer)
    sid = str(d.get("deck") or "")
    if SLIDE_ID_RX.fullmatch(sid):
        new["deck"] = sid
    try:
        new["slideSec"] = max(3.0, min(600.0, float(d.get("slideSec") or 10)))
    except (TypeError, ValueError):
        new["slideSec"] = 10.0
    if new["mode"] == "presentatie":
        meta = slides_get(new.get("deck", ""))
        if not new.get("deck"):
            raise ValueError("kies eerst een presentatie")
        if not meta:
            raise ValueError("presentatie niet gevonden op dit toestel")
        new["deckCount"], new["deckName"] = meta["count"], meta.get("name") or ""
    INFO.clear()
    INFO.update(new)
    if INFO_FILE:
        tmp = INFO_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(INFO, f, ensure_ascii=False)
        os.replace(tmp, INFO_FILE)
    print(f"infoscherm: {INFO['mode']}")
    return dict(INFO)


# ---------- presentaties voor het infoscherm: PowerPoint/PDF -> afbeeldingen (LibreOffice + pdftoppm) ----------
# Omzetten gebeurt waar LibreOffice staat (de cloudserver); een kastje zonder LibreOffice stuurt het bestand daarheen
# en krijgt de dia's terug als zip. De id is een hash van het bestand: dezelfde presentatie = dezelfde map, overal.
SLIDE_EXTS = (".pptx", ".ppt", ".pps", ".ppsx", ".odp", ".pdf")
SLIDE_ID_RX = re.compile(r"[0-9a-f]{16}")
SLIDES_MAX = 12                     # zoveel presentaties bewaren (de oudste ongebruikte gaat eerst weg)
SLIDES_LOCK = threading.Lock()


def slides_dir():
    return os.path.join(DATA, "slides")


def slides_list():
    out = []
    d = slides_dir()
    for sid in (os.listdir(d) if os.path.isdir(d) else []):
        try:
            with open(os.path.join(d, sid, "deck.json"), encoding="utf-8") as f:
                m = json.load(f)
            if m.get("id") == sid and m.get("count"):
                out.append(m)
        except (OSError, ValueError):
            pass
    return sorted(out, key=lambda m: -m.get("created", 0))


def slides_get(sid):
    if not SLIDE_ID_RX.fullmatch(sid or ""):
        return None
    try:
        with open(os.path.join(slides_dir(), sid, "deck.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def slides_tools():
    """(soffice, pdftoppm) als ze op dit toestel staan."""
    return (shutil.which("soffice") or shutil.which("libreoffice"), shutil.which("pdftoppm"))


def slides_convert_local(data, fname):
    """Bestand -> lijst JPEG-dia's (bytes), 1920 px breed."""
    import tempfile
    soffice, pdftoppm = slides_tools()
    if not pdftoppm:
        raise RuntimeError("pdftoppm (poppler-utils) ontbreekt")
    ext = os.path.splitext(fname.lower())[1]
    with tempfile.TemporaryDirectory(prefix="khzs-dia-") as tmp:
        src = os.path.join(tmp, "in" + ext)
        with open(src, "wb") as f:
            f.write(data)
        pdf = src
        if ext != ".pdf":
            if not soffice:
                raise RuntimeError("LibreOffice ontbreekt")
            r = subprocess.run([soffice, "--headless", "--norestore", "-env:UserInstallation=file://" + tmp + "/lo",
                                "--convert-to", "pdf", "--outdir", tmp, src],
                               capture_output=True, text=True, timeout=300, env=dict(os.environ, HOME=tmp))
            pdf = os.path.join(tmp, "in.pdf")
            if not os.path.exists(pdf):
                raise RuntimeError("omzetten mislukt: " + ((r.stderr or r.stdout or "").strip()[-300:] or "geen PDF"))
        r = subprocess.run([pdftoppm, "-jpeg", "-jpegopt", "quality=86", "-scale-to-x", "1920", "-scale-to-y", "-1",
                            "-l", "300", pdf, os.path.join(tmp, "s")], capture_output=True, text=True, timeout=300)
        files = sorted(f for f in os.listdir(tmp) if f.startswith("s-") and f.endswith(".jpg"))
        if not files:
            raise RuntimeError("geen dia's gevonden: " + (r.stderr or "").strip()[-200:])
        out = []
        for fn in files:
            with open(os.path.join(tmp, fn), "rb") as f:
                out.append(f.read())
        return out


def slides_store(sid, name, images):
    d = os.path.join(slides_dir(), sid)
    tmp = d + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for i, img in enumerate(images, 1):
        with open(os.path.join(tmp, f"{i:03d}.jpg"), "wb") as f:
            f.write(img)
    meta = {"id": sid, "name": name[:120], "count": len(images), "created": int(time.time())}
    with open(os.path.join(tmp, "deck.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    with SLIDES_LOCK:
        shutil.rmtree(d, ignore_errors=True)
        os.replace(tmp, d)
        decks = slides_list()
        for m in decks[SLIDES_MAX:]:
            if m["id"] not in (sid, INFO.get("deck")):
                shutil.rmtree(os.path.join(slides_dir(), m["id"]), ignore_errors=True)
    print(f"presentatie {name}: {len(images)} dia's")
    return meta


def slides_zip(sid):
    import zipfile
    d = os.path.join(slides_dir(), sid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for fn in sorted(os.listdir(d)):
            if fn == "deck.json" or re.fullmatch(r"\d{3}\.jpg", fn):
                z.write(os.path.join(d, fn), fn)
    return buf.getvalue()


def slides_from_zip(data):
    import zipfile
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        meta = json.loads(z.read("deck.json"))
        sid = str(meta.get("id") or "")
        if not SLIDE_ID_RX.fullmatch(sid):
            raise ValueError("ongeldige presentatie")
        names = sorted(n for n in z.namelist() if re.fullmatch(r"\d{3}\.jpg", n))
        if not names or len(names) > 300:
            raise ValueError("geen dia's")
        imgs = [z.read(n) for n in names]
    return slides_store(sid, str(meta.get("name") or sid), imgs)


def slides_import(data, fname):
    """Upload verwerken: zelf omzetten als het kan, anders via de publieke server."""
    fname = os.path.basename(fname or "presentatie.pptx")
    ext = os.path.splitext(fname.lower())[1]
    if ext not in SLIDE_EXTS:
        raise ValueError("enkel PowerPoint (.pptx/.ppt/.ppsx), OpenOffice (.odp) of PDF")
    sid = hashlib.sha256(data).hexdigest()[:16]
    have = slides_get(sid)
    if have:
        return have
    name = os.path.splitext(fname)[0]
    soffice, pdftoppm = slides_tools()
    if pdftoppm and (soffice or ext == ".pdf"):
        meta = slides_store(sid, name, slides_convert_local(data, fname))
        if SETTINGS.get("relay_url") and SETTINGS.get("relay_token") and not IS_RELAY:
            threading.Thread(target=slides_push, args=(sid,), daemon=True).start()
        return meta
    if not (SETTINGS.get("relay_url") and SETTINGS.get("relay_token")):
        raise RuntimeError("dit toestel kan zelf geen PowerPoint omzetten en er is geen publieke server ingesteld (Doorsturen)")
    code, body, ctype = cloud_call("POST", "slides/convert?name=" + urllib.parse.quote(fname), data,
                                   "application/octet-stream", timeout=360)
    if code != 200 or not ctype.startswith("application/zip"):
        try:
            err = json.loads(body).get("error")
        except ValueError:
            err = None
        raise RuntimeError("omzetten op de publieke server mislukt: " + (err or f"HTTP {code}"))
    return slides_from_zip(body)


def slides_push(sid):
    """Kastje -> publieke server, zodat die dezelfde presentatie kan tonen (enkel als ze er nog niet is)."""
    code, body, _ = cloud_call("GET", "slides/has?id=" + sid, timeout=20)
    try:
        if code == 200 and json.loads(body).get("has"):
            return True
    except ValueError:
        pass
    code, body, _ = cloud_call("POST", "slides/put", slides_zip(sid), "application/zip", timeout=300)
    if code != 200:
        print(f"presentatie {sid} naar de publieke server: HTTP {code}")
    return code == 200


def slides_delete(sid):
    if not SLIDE_ID_RX.fullmatch(sid or ""):
        raise ValueError("ongeldige presentatie")
    if INFO.get("mode") == "presentatie" and INFO.get("deck") == sid:
        raise ValueError("deze presentatie wordt nu getoond – zet eerst het infoscherm om")
    with SLIDES_LOCK:
        shutil.rmtree(os.path.join(slides_dir(), sid), ignore_errors=True)


# ---------- beheer op afstand: de publieke server stuurt verzoeken door naar een kastje (tunnel over HTTPS) ----------
# Het kastje haalt verzoeken op met long-polling (uitgaand HTTPS, werkt achter elke firewall/portal waar het web open is),
# voert ze lokaal uit en stuurt het antwoord terug. Toegang: beheerwachtwoord cloudserver + beheerderscode van het kastje.
TUNNEL_NAME_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
TUNNEL_FWD_HEADERS = ("Content-Type", "X-Admin-Token", "X-Filename", "Accept", "Range")


class TunnelHub:
    """Relay-kant: wachtrij per kastje en openstaande verzoeken."""
    def __init__(self):
        self.lock = threading.Lock()
        self.boxes = {}          # naam -> {"q", "seen", "info"}
        self.pending = {}        # id -> {"ev", "resp"}

    def poll(self, name, info, wait=25.0):
        with self.lock:
            b = self.boxes.setdefault(name, {"q": queue.Queue(), "seen": 0, "info": {}})
            b["seen"], b["info"] = time.time(), info or {}
        end = time.time() + wait
        while True:
            try:
                req = b["q"].get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                return None
            if time.time() - req["at"] <= 30 and req["id"] in self.pending:    # oude verzoeken nooit nog uitvoeren
                return req
            if time.time() >= end:
                return None

    def request(self, name, req, timeout=75.0):
        with self.lock:
            b = self.boxes.get(name)
        if not b or time.time() - b["seen"] > 60:
            raise LookupError(f"kastje '{name}' is niet verbonden (staat 'beheer op afstand' aan en heeft het internet?)")
        rid = _secrets.token_hex(12)
        ev = threading.Event()
        self.pending[rid] = {"ev": ev, "resp": None}
        req.update(id=rid, at=time.time())
        b["q"].put(req)
        try:
            if not ev.wait(timeout):
                raise TimeoutError("het kastje antwoordt niet")
            return self.pending[rid]["resp"]
        finally:
            self.pending.pop(rid, None)

    def reply(self, rid, resp):
        p = self.pending.get(rid)
        if p:
            p["resp"] = resp
            p["ev"].set()

    def list(self):
        with self.lock:
            return [{"name": n, "online": time.time() - b["seen"] < 60, "lastSeen": int(time.time() - b["seen"]),
                     **{k: v for k, v in b["info"].items() if k in ("version", "role", "hostname", "label", "simulated", "connected")}}
                    for n, b in sorted(self.boxes.items())]


TUNNEL = TunnelHub()


def release_dir():
    return os.path.join(os.path.dirname(os.path.abspath(SETTINGS_FILE)), "releases")


def release_info():
    """Het laatst naar de cloudserver gestuurde pakket (voor de kastjes)."""
    try:
        with open(os.path.join(release_dir(), "latest.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def release_store(blob):
    try:
        import zipfile as _zf
        meta = json.loads(_zf.ZipFile(io.BytesIO(blob)).read("release.json"))
    except Exception:
        return None
    os.makedirs(release_dir(), exist_ok=True)
    tmp = os.path.join(release_dir(), "latest.zip.tmp")
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, os.path.join(release_dir(), "latest.zip"))
    info = {"version": meta.get("version"), "built": meta.get("built"), "size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(), "stored": int(time.time())}
    with open(os.path.join(release_dir(), "latest.json"), "w", encoding="utf-8") as f:
        json.dump(info, f)
    print(f"pakket {info['version']} bewaard voor de kastjes")
    return info

TUNNEL_INJECT = """<script>/* via beheer op afstand: alle adressen van dit kastje beginnen met __BASE__ */
(function(){var B='__BASE__';window.LT_BASE=B;
function fx(u){return (typeof u==='string'&&u.charAt(0)==='/'&&u.charAt(1)!=='/'&&u.indexOf(B+'/')!==0)?B+u:u}
var f=window.fetch;window.fetch=function(u,o){if(u&&u.url&&!(typeof u==='string'))return f.call(this,u,o);return f.call(this,fx(u),o)};
window.EventSource=function(){this.readyState=2;this.addEventListener=function(){};this.close=function(){}};
[[HTMLScriptElement,'src'],[HTMLIFrameElement,'src'],[HTMLImageElement,'src'],[HTMLLinkElement,'href'],[HTMLAnchorElement,'href']].forEach(function(p){
  var d=Object.getOwnPropertyDescriptor(p[0].prototype,p[1]);if(!d||!d.set)return;
  Object.defineProperty(p[0].prototype,p[1],{get:d.get,set:function(v){d.set.call(this,fx(v))},configurable:true})});
var sa=Element.prototype.setAttribute;Element.prototype.setAttribute=function(n,v){if(n==='src'||n==='href'||n==='action')v=fx(v);return sa.call(this,n,v)};
function fix(el){['src','href','action'].forEach(function(a){var v=el.getAttribute&&el.getAttribute(a);if(v&&fx(v)!==v)sa.call(el,a,fx(v))})}
new MutationObserver(function(ms){ms.forEach(function(m){m.addedNodes.forEach(function(n){if(n.nodeType===1){fix(n);if(n.querySelectorAll)n.querySelectorAll('[src],[href],[action]').forEach(fix)}})})}).observe(document.documentElement,{subtree:true,childList:true});
document.addEventListener('DOMContentLoaded',function(){document.querySelectorAll('[src],[href],[action]').forEach(fix);
  var b=document.createElement('div');b.style.cssText='position:fixed;left:50%;transform:translateX(-50%);bottom:8px;z-index:9999;background:#7c3aed;color:#fff;font:600 12px system-ui;padding:4px 12px;border-radius:999px;opacity:.9;pointer-events:none';
  b.textContent='Beheer op afstand: '+B.split('/').pop();document.body.appendChild(b)});
})();</script>"""


TLS = {"port": None, "fingerprint": None, "error": None}


def tls_context():
    """Zelfondertekend certificaat (10 jaar) in DATA/tls; wordt bij de eerste start aangemaakt met openssl."""
    import ssl
    d = os.path.join(DATA, "tls")
    cert, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        os.makedirs(d, exist_ok=True)
        host = socket.gethostname()
        san = f"subjectAltName=DNS:{host}.local,DNS:{host},DNS:localhost,IP:127.0.0.1,IP:10.42.0.1,IP:10.43.0.1"
        r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                            "-keyout", key, "-out", cert, "-subj", f"/CN={host}.local/O=HZS Timing", "-addext", san],
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            raise OSError("certificaat maken mislukt: " + r.stderr.decode("utf-8", "replace")[-200:])
        os.chmod(key, 0o600)
        print("zelfondertekend certificaat aangemaakt:", cert)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    der = ssl.PEM_cert_to_DER_cert(open(cert, encoding="ascii").read())
    TLS["fingerprint"] = ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
    return ctx


def https_server(handler, bind):
    """Beheer (en alle pagina's) ook via HTTPS; HTTP blijft werken."""
    want = int(SETTINGS.get("https_port") or 0)
    port = want if want > 0 else (443 if (want == 0 and AGENT_URL) else None)
    if not port:
        return
    try:
        ctx = tls_context()
        srv = ThreadingHTTPServer((bind, port), handler)
        srv.daemon_threads = True
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)   # handshake in de thread per verbinding
        TLS["port"] = port
        print(f"HTTPS op poort {port} (vingerafdruk {TLS['fingerprint'][:23]}…)")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    except Exception as e:
        TLS["error"] = str(e)
        print("HTTPS niet gestart:", e)


TUNNEL_STATUS = {"connected": False, "lastOk": None, "error": None}


def tunnel_client(port):
    """Lokale kant: verzoeken van de publieke server ophalen en lokaal uitvoeren (4 tegelijk, voor console + rest)."""
    def worker(n):
        conn, backoff = None, 2.0
        while True:
            url, tok = (SETTINGS.get("relay_url") or "").rstrip("/"), SETTINGS.get("relay_token") or ""
            if not (SETTINGS.get("remote_admin") and url and tok):
                conn = None
                time.sleep(5)
                continue
            u = urllib.parse.urlsplit(url)
            try:
                if conn is None:
                    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
                    conn = cls(u.hostname, u.port, timeout=45)
                info = {"version": VERSION, "role": ROLE, "hostname": socket.gethostname(), "label": SETTINGS.get("device_label") or ""}
                if STATE is not None:
                    info["connected"] = bool(STATE.state_json(now_fn()).get("connected"))
                    info["simulated"] = bool(STATE_SOURCE.get("sim"))
                conn.request("POST", (u.path or "") + "/tunnel/poll",
                             body=json.dumps({"name": socket.gethostname().lower(), "info": info}),
                             headers={"X-Ingest-Token": tok, "Content-Type": "application/json",
                                      "User-Agent": f"khzs-livetiming/{VERSION}"})
                r = conn.getresponse()
                data = r.read()
                if r.status != 200:
                    raise OSError(f"tunnel: HTTP {r.status}")
                TUNNEL_STATUS.update(connected=True, lastOk=time.time(), error=None)
                req = json.loads(data or b"{}").get("req")
                backoff = 2.0
                if not req:
                    continue
                resp = tunnel_local(port, req)
                conn.request("POST", (u.path or "") + "/tunnel/reply", body=json.dumps(resp),
                             headers={"X-Ingest-Token": tok, "Content-Type": "application/json"})
                conn.getresponse().read()
            except (OSError, ValueError, http.client.HTTPException) as e:
                if n == 0:
                    print("beheer op afstand:", e)
                    TUNNEL_STATUS.update(connected=False, error=str(e))
                conn = None
                time.sleep(backoff)
                backoff = min(15.0, backoff * 2)          # na een storing binnen 15 s opnieuw verbonden

    for i in range(4):
        threading.Thread(target=worker, args=(i,), daemon=True).start()


def tunnel_local(port, req):
    """Eén doorgestuurd verzoek lokaal uitvoeren. Nooit als 'lokaal' vertrouwd: de beheerderscode blijft nodig."""
    h = {k: v for k, v in (req.get("headers") or {}).items() if k in TUNNEL_FWD_HEADERS}
    h["X-Forwarded-For"] = "beheer-op-afstand"
    body = base64.b64decode(req.get("body") or "") if req.get("body") else None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=70)
    try:
        conn.request(req.get("method", "GET"), req.get("path", "/"), body=body, headers=h)
        r = conn.getresponse()
        data = r.read(60 * 1024 * 1024)
        hdr = {k: v for k, v in r.getheaders() if k in ("Content-Type", "Cache-Control", "Content-Disposition", "Location")}
        return {"id": req["id"], "status": r.status, "headers": hdr, "body": base64.b64encode(data).decode()}
    except OSError as e:
        return {"id": req["id"], "status": 502, "headers": {"Content-Type": "application/json"},
                "body": base64.b64encode(json.dumps({"error": f"lokale server: {e}"}).encode()).decode()}
    finally:
        conn.close()


# ---------- cloudbeheer: lokale server -> https://<relay>/admin/* -> beheerdienst (root) op de cloudserver ----------
CLOUD_AGENT = ("127.0.0.1", 8092)
CLOUD_AGENT_KEY_FILE = os.environ.get("LT_AGENT_KEY_FILE", "/etc/livetiming-agent.key")
ADMIN_FAIL_LOCK = threading.Lock()
RELAY_ADMIN_KEYS = [k for k in DEFAULT_SETTINGS if k not in ("relay_token", "admin_token", "relay_url")]


def cloud_agent_call(method, path, body=None, headers=None, timeout=70):
    """Relay-modus: verzoek doorgeven aan de beheerdienst op dezelfde server."""
    try:
        key = open(CLOUD_AGENT_KEY_FILE).read().strip()
    except OSError:
        return 503, b'{"ok":false,"error":"beheerdienst niet geinstalleerd op de cloudserver (pve_deploy.py deploy)"}', "application/json"
    h = {"X-Agent-Key": key, "Host": "127.0.0.1"}
    h.update(headers or {})
    conn = http.client.HTTPConnection(*CLOUD_AGENT, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        return r.status, r.read(), r.getheader("Content-Type") or "application/json"
    except OSError as e:
        return 502, json.dumps({"ok": False, "error": f"beheerdienst onbereikbaar: {e}"}).encode(), "application/json"
    finally:
        conn.close()


def cloud_call(method, sub, body=None, ctype="application/json", timeout=70):
    """Lokale server: beheerverzoek naar de publieke server (HTTPS, zelfde token als het doorsturen)."""
    url = (SETTINGS.get("relay_url") or "").rstrip("/")
    tok = SETTINGS.get("relay_token") or ""
    if not url or not tok:
        return 400, json.dumps({"ok": False, "error": "eerst het adres en token van de publieke server instellen (Doorsturen)"}).encode(), "application/json"
    u = urllib.parse.urlsplit(url)
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(u.hostname, u.port, timeout=timeout)
    try:
        conn.request(method, (u.path or "") + "/admin/" + sub, body=body,
                     headers={"X-Ingest-Token": tok, "Content-Type": ctype, "X-Who": socket.gethostname()[:40],
                              "User-Agent": f"khzs-livetiming/{VERSION}"})
        r = conn.getresponse()
        return r.status, r.read(), r.getheader("Content-Type") or "application/json"
    except OSError as e:
        return 502, json.dumps({"ok": False, "error": f"publieke server onbereikbaar: {e}"}).encode(), "application/json"
    finally:
        conn.close()


def own_release_zip():
    """Eigen code als releasepakket (zelfde formaat als tools/build_release.py) om de cloudserver bij te werken."""
    sys.path.insert(0, os.path.join(BASE, "tools"))
    import build_release
    return build_release.build_bytes(BASE)


def make_handler(state, hub, relay=None):
    """relay=None: gewone server op de laptop. relay=RelayStore: publieke server (ontvangt van de laptop)."""
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send(self, status, body, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _view(self):
            q = self.path.partition("?")[2]
            return "jury" if "view=jury" in q else "publiek"

        # ----- relay: aanmelding -----
        def _cookie(self, name):
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v
            return ""

        def _cloud_admin(self):
            """Relay: aangemeld met het beheerwachtwoord van de cloudserver."""
            apw = SETTINGS.get("cloud_admin_password") or ""
            return bool(relay is not None and apw and hmac.compare_digest(self._cookie("lt_a"), session_token(apw + "|beheer")))

        def _authed(self):
            if relay is None:
                return True
            if self._cloud_admin():
                return True
            pw = SETTINGS.get("jury_password") or ""
            if not pw:
                return False
            cookies = self.headers.get("Cookie", "")
            for part in cookies.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "lt_s" and hmac.compare_digest(v, session_token(pw)):
                    return True
            return False

        def _require_login(self, as_json=False):
            """True = toegang; anders is het antwoord al verstuurd."""
            if self._authed():
                return True
            if not (SETTINGS.get("jury_password") or ""):
                self.send_error(503, "Geen jury-wachtwoord ingesteld op de server (jury_password)")
                return False
            if as_json:
                body = b'{"error":"login"}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(302)
                self.send_header("Location", "/login?next=" + urllib.parse.quote(self.path, safe=""))
                self.send_header("Content-Length", "0")
                self.end_headers()
            return False

        def _login_page(self, nxt, err=""):
            body = (LOGIN_HTML.replace("__TITLE__", "Live timing – aanmelden")
                    .replace("__NEXT__", nxt.replace('"', "").replace("<", ""))
                    .replace("__ERR__", f'<p class="err">{err}</p>' if err else "")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _safe_next(self, nxt):
            return nxt if (nxt.startswith("/") and not nxt.startswith("//")) else "/jury"

        def _captive(self, path):
            """Hotspotmodus: telefoons en laptops die hun 'internetcheck' doen, naar het beheer sturen."""
            if relay is not None or not AGENT_STATUS["ap"]:
                return False
            host = (self.headers.get("Host") or "").split(":")[0]
            ours = host in ("", AGENT_STATUS["apIp"], "khzs-timing", "khzs-timing.local", "localhost", "127.0.0.1")
            if path in CAPTIVE_PATHS or not ours:
                self.send_response(302)
                self.send_header("Location", f"http://{AGENT_STATUS['apIp']}/system")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return True
            return False

        def _system_api(self, method):
            path = self.path.split("?")[0]
            sub = self.path[len("/api/system"):] or "/"
            local_kiosk = (self.client_address[0] in ("127.0.0.1", "::1") and not self.headers.get("Origin")
                           and not any(self.headers.get(h) for h in ("Cf-Connecting-Ip", "X-Forwarded-For")))
            if method == "GET" and sub.split("?")[0] == "/display" and local_kiosk:
                pass                                     # HDMI-scherm (statuspagina) mag zijn eigen weergave-info lezen
            elif not self._is_admin():
                return self._json({"error": "beheerderscode nodig"}, 403) if method != "GET" or sub.split("?")[0] != "/status" \
                    else self._json({"agent": bool(AGENT_URL), "locked": True, "version": VERSION})
            body = None
            hdr = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length", 0) or 0)
                if n > 200 * 1024 * 1024:
                    return self._json({"error": "bestand te groot"}, 413)
                body = self.rfile.read(n) if n else b""
                hdr = {"Content-Type": self.headers.get("Content-Type", "application/json"),
                       "Content-Length": str(len(body))}
                for h in ("X-Filename",):
                    if self.headers.get(h):
                        hdr[h] = self.headers.get(h)
            st, ctype, data = agent_call(method, sub, body, hdr, timeout=120)
            self.send_response(st)
            self.send_header("Content-Type", ctype or "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            if self._captive(path):
                return
            if relay is None and path in ("/system", "/system.html"):
                self.send_response(302)
                self.send_header("Location", "/settings#netwerk")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if relay is None and path in ("/display", "/display.html"):
                return self._file(os.path.join(STATIC, "display.html"), "text/html; charset=utf-8")
            if relay is None and ROLE != "server" and path in ("/", "/index.html", "/publiek", "/jury", "/callroom"):
                self.send_response(302)
                self.send_header("Location", "/display")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if relay is None and path.startswith("/api/system"):
                return self._system_api("GET")
            if relay is None and path.startswith("/api/cloud/"):
                return self._cloud("GET")
            if path.startswith("/vendor/") and re.fullmatch(r"/vendor/[a-z0-9.-]+\.(js|css)", path):
                return self._file(os.path.join(STATIC, "vendor", path[8:]),
                                  "text/css" if path.endswith(".css") else "application/javascript")
            if path == "/api/stats" and STATS is not None and (relay is None and self._is_admin()):
                return self._json(dict(STATS.report(), ok=True))
            if path == "/api/info":
                return self._json(INFO)
            if path == "/api/slides":
                if not (self._cloud_admin() if relay is not None else self._is_admin()):
                    return self._json({"ok": False, "error": "beheerderscode nodig"}, 403)
                so, pp = slides_tools()
                return self._json({"ok": True, "decks": slides_list(), "local": bool(so and pp), "pdf": bool(pp),
                                   "viaCloud": bool(relay is None and SETTINGS.get("relay_url") and SETTINGS.get("relay_token"))})
            m_ = re.fullmatch(r"/slides/([0-9a-f]{16})/(\d{3})\.jpg", path)
            if m_:
                return self._file(os.path.join(slides_dir(), m_.group(1), m_.group(2) + ".jpg"), "image/jpeg",
                                  "public, max-age=31536000, immutable")
            if path == "/api/tls" and relay is None:
                return self._json(TLS)
            if path == "/api/peers" and relay is None:
                return self._json({"peers": peers_json()})
            if path == "/api/udp" and relay is None:
                d = dict(UDP_STATUS, lastAgo=round(time.time() - UDP_STATUS["lastAt"], 1) if UDP_STATUS["lastAt"] else None)
                d.pop("lastAt", None)
                d["seen"] = sorted(({"iface": e["iface"], "from": e["from"], "count": e["count"],
                                     "ago": round(time.time() - e["last"], 1)} for e in UDP_STATUS["seen"].values()),
                                   key=lambda e: e["ago"])
                if not self._is_admin():
                    d.pop("lastRejected", None)
                return self._json(d)
            if path in ("/info", "/info.html"):
                return self._file(os.path.join(STATIC, "info.html"), "text/html; charset=utf-8")
            if path == "/infoscreen.js":
                return self._file(os.path.join(STATIC, "infoscreen.js"), "application/javascript")
            if path.startswith("/img/") and re.fullmatch(r"/img/[a-z0-9-]+\.(png|svg)", path):
                return self._file(os.path.join(STATIC, "img", path[5:]), "image/svg+xml" if path.endswith(".svg") else "image/png")
            if path == "/api/version":
                others = [p for p in peers_json() if p["role"] == "server"] if relay is None else []
                return self._json({"version": VERSION, "agent": bool(AGENT_URL), "role": ROLE if relay is None else "relay",
                                   "label": SETTINGS.get("device_label") or "", "hostname": socket.gethostname(),
                                   "otherServers": [{"name": p["label"] or p["name"], "ip": p["ip"]} for p in others]})
            if relay is not None:
                if path == "/login":
                    q = urllib.parse.parse_qs(self.path.partition("?")[2])
                    pw = SETTINGS.get("jury_password") or ""
                    key = q.get("key", [""])[0]
                    if key and pw and hmac.compare_digest(key, pw):          # schermkastje met schermsleutel
                        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto", "").lower() == "https") else ""
                        self.send_response(303)
                        self.send_header("Set-Cookie", f"lt_s={session_token(pw)}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax{secure}")
                        self.send_header("Location", self._safe_next(q.get("next", ["/jury"])[0]))
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if key:
                        with LOGIN_FAIL_LOCK:
                            time.sleep(1.5)
                    return self._login_page(self._safe_next(q.get("next", ["/jury"])[0]))
                if path == "/logout":
                    self.send_response(302)
                    self.send_header("Set-Cookie", "lt_s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
                    self.send_header("Set-Cookie", "lt_a=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
                    nxt = urllib.parse.parse_qs(self.path.partition("?")[2]).get("next", ["/"])[0]
                    self.send_header("Location", "/login?next=/settings" if nxt == "/login" else self._safe_next(nxt))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/api/relay":
                    return self._json({"relay": True, "lastIngestAgo": relay.state_json().get("relayAgo")})
                if path.startswith("/kastje/"):
                    return self._tunnel_proxy("GET")
                if path in ("/tunnel/release", "/tunnel/release.zip"):
                    tok = SETTINGS.get("relay_token") or ""
                    if not tok or not hmac.compare_digest(self.headers.get("X-Ingest-Token", ""), tok):
                        with ADMIN_FAIL_LOCK:
                            time.sleep(1.5)
                        return self._json({"ok": False, "error": "ongeldig token"}, 403)
                    info = release_info()
                    if path.endswith(".zip"):
                        if not info:
                            return self._json({"ok": False, "error": "geen pakket"}, 404)
                        return self._file(os.path.join(release_dir(), "latest.zip"), "application/zip")
                    return self._json({"ok": True, "release": info})
                if path == "/api/kastjes":
                    if not self._cloud_admin():
                        return self._json({"ok": False, "error": "beheerwachtwoord nodig"}, 403)
                    return self._json({"ok": True, "boxes": TUNNEL.list(), "release": release_info()})
                if path == "/api/whoami":
                    return self._json({"relay": True, "admin": self._cloud_admin(), "jury": self._authed(),
                                       "adminConfigured": bool(SETTINGS.get("cloud_admin_password"))})
                if path == "/api/stats":
                    if not self._cloud_admin():
                        return self._json({"ok": False, "error": "beheerwachtwoord nodig"}, 403)
                    return self._json(dict(STATS.report() if STATS else {}, ok=True))
                if path.startswith("/admin/"):
                    return self._admin("GET")
                protected_pages = ("/jury", "/settings", "/settings.html")      # oproepkamer is publiek
                protected_api = ("/api/db", "/api/settings")
                if path in protected_pages and not self._require_login():
                    return
                if (path in protected_api or (path in ("/state", "/history", "/events") and self._view() == "jury"))                         and not self._require_login(as_json=True):
                    return
                if path == "/api/callroom":
                    return self._json(relay.callroom or {"upcoming": [], "count": 0, "matched": False,
                                                          "db": {"loadedAt": None, "loadedFile": None, "heats": 0}})
                if path == "/api/db":
                    return self._json(dict(relay.db or {}, canEdit=False, relay=True))
                if path == "/api/settings":
                    st = (relay.state or {}).get("settings") or {}
                    return self._json({"settings": st, "defaults": {k: v for k, v in DEFAULT_SETTINGS.items() if k not in SECRET_KEYS},
                                       "canEdit": False, "tokenSet": False, "relay": True})
            if path == "/state":
                d = state.state_json(now_fn())
                return self._json(public_view("state", d) if self._view() == "publiek" else d)
            if path == "/history":
                d = state.history_json()
                return self._json(public_view("history", d) if self._view() == "publiek" else d)
            if path == "/events":
                return self._sse()
            if path in ("/", "/index.html", "/publiek", "/jury"):     # root = publieke weergave (geen omleiding)
                return self._file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
            if path in ("/settings", "/settings.html"):
                return self._file(os.path.join(STATIC, "settings.html"), "text/html; charset=utf-8")
            if relay is None and path in ("/simulator", "/simulator.html"):
                try:
                    html = open(os.path.join(STATIC, "simulator.html"), encoding="utf-8").read()
                except OSError:
                    return self.send_error(404)
                html = html.replace("<script>", "<script>window.SIM_API='/api/sim';</script><script>", 1)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if relay is None and path == "/api/sim/state":
                if not self._is_admin():
                    return self._json({"ok": False, "error": "beheerderscode nodig"}, 403)
                if not SIM.running():
                    return self._json(dict(SIM.status(), ok=True))
                return self._json(dict(SIM.eng.snapshot(), running=True))
            if relay is None and path == "/api/sim":
                return self._json(dict(SIM.status(), canEdit=self._is_admin()))
            if path == "/api/callroom":
                st = public_view("state", state.state_json(now_fn()))
                return self._json(callroom_json(st))
            if path in ("/callroom", "/callroom.html"):
                return self._file(os.path.join(STATIC, "callroom.html"), "text/html; charset=utf-8")
            if path == "/api/relay":
                tun = dict(TUNNEL_STATUS, enabled=bool(SETTINGS.get("remote_admin")),
                           ago=round(time.time() - TUNNEL_STATUS["lastOk"]) if TUNNEL_STATUS["lastOk"] else None)
                return self._json(dict(PUSHER.status(), enabled=True, tunnel=tun) if PUSHER
                                  else {"enabled": False, "tunnel": tun})
            if path == "/api/programma":
                return self._json(dict(PROG_STATUS, heats=len(PROG_SCHEDULE), canEdit=self._is_admin()))
            if path == "/api/db":
                return self._json(dict(db_status_json(), canEdit=self._is_admin()))
            if path == "/api/settings":
                return self._json({"settings": {k: v for k, v in SETTINGS.items() if k not in SECRET_KEYS},
                                   "defaults": {k: v for k, v in DEFAULT_SETTINGS.items() if k not in SECRET_KEYS},
                                   "canEdit": self._is_admin(), "tokenSet": bool(SETTINGS["admin_token"]),
                                   "defaultToken": SETTINGS["admin_token"] == "zwemclub" and not SETTINGS.get("trust_localhost", True)})
            self.send_error(404)

        def _slides_post(self, path0):
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n <= 0 or n > 60 * 1024 * 1024:
                return self._json({"ok": False, "error": "leeg of groter dan 60 MB"}, 400)
            body = self.rfile.read(n)
            try:
                if path0 == "/api/slides/delete":
                    slides_delete(str(json.loads(body or b"{}").get("id") or ""))
                    return self._json({"ok": True, "decks": slides_list()})
                meta = slides_import(body, urllib.parse.unquote(self.headers.get("X-Filename", "") or ""))
            except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._json({"ok": True, "deck": meta, "decks": slides_list()})

        def _tunnel_proxy(self, method):
            """Relay: /kastje/<naam>/<pad> -> verzoek naar dat kastje (enkel voor de beheerder van de cloudserver)."""
            if not self._cloud_admin():
                if method == "GET" and "/api/" not in self.path:
                    return self._require_login() and None
                return self._json({"ok": False, "error": "beheerwachtwoord van de cloudserver nodig"}, 403)
            origin = self.headers.get("Origin") or ""
            if origin and urllib.parse.urlsplit(origin).netloc != (self.headers.get("Host") or ""):
                return self._json({"ok": False, "error": "andere website"}, 403)
            rest = self.path[len("/kastje/"):]
            name, _, sub = rest.partition("/")
            name = name.lower()
            if not TUNNEL_NAME_RX.match(name):
                return self.send_error(404)
            if not sub:
                self.send_response(302)
                self.send_header("Location", f"/kastje/{name}/settings")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n > 60 * 1024 * 1024:
                return self._json({"ok": False, "error": "te groot"}, 413)
            body = self.rfile.read(n) if n else b""
            req = {"method": method, "path": "/" + sub,
                   "headers": {k: self.headers.get(k) for k in TUNNEL_FWD_HEADERS if self.headers.get(k)},
                   "body": base64.b64encode(body).decode() if body else ""}
            try:
                resp = TUNNEL.request(name, req)
            except (LookupError, TimeoutError) as e:
                return self._json({"ok": False, "error": str(e)}, 504)
            data = base64.b64decode(resp.get("body") or "")
            hdr = resp.get("headers") or {}
            ctype = hdr.get("Content-Type", "application/octet-stream")
            if ctype.startswith("text/html"):
                inj = TUNNEL_INJECT.replace("__BASE__", f"/kastje/{name}").encode("utf-8")
                i = data.find(b"<head>")
                data = data[:i + 6] + inj + data[i + 6:] if i >= 0 else inj + data
            self.send_response(int(resp.get("status") or 502))
            for k, v in hdr.items():
                if k == "Location" and v.startswith("/") and not v.startswith("//"):
                    v = f"/kastje/{name}" + v
                if k != "Content-Type":
                    self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _cloud(self, method):
            """Lokale server: beheer van de publieke server, enkel voor de beheerder van dit toestel."""
            if not self._is_admin():
                return self._json({"ok": False, "error": "geen toegang (beheerderscode nodig)"}, 403)
            path, _, q = self.path.partition("?")
            sub = path[len("/api/cloud/"):]
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(n) if n else None
            if sub == "release-upload" and method == "POST":
                try:
                    blob, ver = own_release_zip()
                except Exception as e:
                    return self._json({"ok": False, "error": f"pakket maken mislukt: {e}"}, 500)
                url = (SETTINGS.get("relay_url") or "").rstrip("/")
                u = urllib.parse.urlsplit(url)
                conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(u.hostname, u.port, timeout=120)
                try:
                    conn.request("POST", (u.path or "") + "/tunnel/release-upload", body=blob,
                                 headers={"X-Ingest-Token": SETTINGS.get("relay_token") or "", "Content-Type": "application/zip"})
                    r = conn.getresponse()
                    return self._send(409 if r.status == 403 else r.status, r.read(), "application/json")
                except OSError as e:
                    return self._json({"ok": False, "error": f"publieke server onbereikbaar: {e}"}, 502)
                finally:
                    conn.close()
            if sub == "update-self" and method == "POST":
                try:
                    blob, ver = own_release_zip()
                except Exception as e:
                    return self._json({"ok": False, "error": f"pakket maken mislukt: {e}"}, 500)
                code, data, ctype = cloud_call("POST", "update", blob, "application/zip", timeout=180)
                return self._send(409 if code == 403 else code, data, ctype)
            code, data, ctype = cloud_call(method, sub + ("?" + q if q else ""), body,
                                           self.headers.get("Content-Type", "application/json"))
            # 403 van de cloudserver (token/consolewachtwoord) is geen lokaal toegangsprobleem
            return self._send(409 if code == 403 else code, data, ctype)

        def _admin(self, method):
            """Relay: beheer vanaf de lokale server. Token = relay_token; console vraagt daarnaast een eigen wachtwoord."""
            tok = SETTINGS.get("relay_token") or ""
            if not tok or not hmac.compare_digest(self.headers.get("X-Ingest-Token", ""), tok):
                with ADMIN_FAIL_LOCK:
                    time.sleep(1.5)
                return self._json({"ok": False, "error": "ongeldig token"}, 403)
            path, _, q = self.path.partition("?")
            sub = path[len("/admin"):]
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n > 60 * 1024 * 1024:
                return self._json({"ok": False, "error": "te groot"}, 413)
            body = self.rfile.read(n) if n else b""
            if sub == "/settings":
                if method == "POST":
                    try:
                        new = json.loads(body or b"{}")
                        if not isinstance(new, dict):
                            raise ValueError
                    except ValueError:
                        return self._json({"ok": False, "error": "ongeldige JSON"}, 400)
                    bad = [k for k in new if k not in RELAY_ADMIN_KEYS]
                    if bad:
                        return self._json({"ok": False, "error": "niet wijzigbaar vanop afstand: " + ", ".join(bad)}, 400)
                    if "jury_password" in new and not new["jury_password"]:
                        new.pop("jury_password")
                    errors = update_settings(new)
                    if errors:
                        return self._json({"ok": False, "error": ", ".join(errors)}, 400)
                    print("instellingen gewijzigd via beheer:", ", ".join(sorted(new)))
                viewers = STATS.now() if STATS else {}
                st = relay.state_json()
                return self._json({"ok": True, "version": VERSION,
                                   "settings": {k: v for k, v in SETTINGS.items() if k in RELAY_ADMIN_KEYS and k not in SECRET_KEYS},
                                   "juryPasswordSet": bool(SETTINGS.get("jury_password")),
                                   "lastIngestAgo": st.get("relayAgo"), "history": len(relay.history or []),
                                   "viewers": viewers})
            if sub == "/info" and method == "POST":
                try:
                    new = info_set(json.loads(body or b"{}"), "lokaal")
                except ValueError as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                hub.send("info", new)
                return self._json({"ok": True, "info": new})
            if sub == "/slides/has":
                return self._json({"ok": True, "has": bool(slides_get(urllib.parse.parse_qs(q).get("id", [""])[0]))})
            if sub == "/slides/convert" and method == "POST":
                try:
                    meta = slides_import(body, urllib.parse.parse_qs(q).get("name", ["presentatie.pptx"])[0])
                    data = slides_zip(meta["id"])
                except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                return self._send(200, data, "application/zip")
            if sub == "/slides/put" and method == "POST":
                try:
                    meta = slides_from_zip(body)
                except Exception as e:
                    return self._json({"ok": False, "error": f"ongeldig pakket: {e}"}, 400)
                return self._json({"ok": True, "deck": meta})
            if sub == "/stats":
                return self._json(dict(STATS.report() if STATS else {}, ok=True))
            if sub == "/cache/clear" and method == "POST":
                relay.clear()
                print("cache gewist via beheer")
                return self._json({"ok": True})
            if not (sub in ("/status", "/logs", "/restart", "/update", "/update/github", "/console/open", "/console/password")
                    or re.fullmatch(r"/console/[A-Za-z0-9_-]{20,64}/(read|write|resize|close)", sub)):
                return self._json({"ok": False, "error": "onbekend"}, 404)
            who = (self.headers.get("X-Who", "?")[:40] + " via " +
                   (self.headers.get("Cf-Connecting-Ip") or self.client_address[0]))
            code, data, ctype = cloud_agent_call(method, sub + ("?" + q if q else ""), body or None,
                                                 {"X-Who": who, "Content-Type": self.headers.get("Content-Type", "application/json")})
            if sub == "/update" and code == 200 and body:
                release_store(body)               # zelfde pakket ook aanbieden aan de kastjes
            return self._send(code, data, ctype)

        def _is_admin(self):
            # via cloudflared/proxy komt alles van localhost binnen: dan enkel met token
            proxied = any(self.headers.get(h) for h in ("Cf-Connecting-Ip", "X-Forwarded-For", "Cf-Ray"))
            local = self.client_address[0] in ("127.0.0.1", "::1") and not proxied
            origin = self.headers.get("Origin") or ""
            if local and origin:
                # een pagina van een andere website (bv. in de kiosk- of hotspot-browser) is geen beheerder
                o = urllib.parse.urlsplit(origin)
                local = o.hostname in ("localhost", "127.0.0.1") and (o.port or 80) == (self.server.server_address[1] or 80)
            tok = SETTINGS["admin_token"]
            given = self.headers.get("X-Admin-Token")
            if given and tok and hmac.compare_digest(given, tok):
                return True
            if given:                                    # foute code: vertragen tegen raden
                with ADMIN_FAIL_LOCK:
                    time.sleep(1.0)
                return False
            # systeemdienst en khzs-commando op het kastje zelf: met de sleutel van de systeemdienst
            ak = self.headers.get("X-Agent-Key")
            if ak and AGENT_URL and not origin:
                try:
                    if hmac.compare_digest(ak, agent_key()):
                        return True
                except OSError:
                    pass
            return local and bool(SETTINGS.get("trust_localhost", True))

        def do_POST(self):
            path0 = self.path.split("?")[0]
            if relay is None and path0.startswith("/api/system"):
                return self._system_api("POST")
            if relay is None and path0.startswith("/api/cloud/"):
                return self._cloud("POST")
            if relay is not None:
                if path0.startswith("/kastje/"):
                    return self._tunnel_proxy("POST")
                if path0 == "/tunnel/release-upload":
                    # pakket enkel bewaren voor de kastjes (zonder de cloudserver zelf bij te werken)
                    tok = SETTINGS.get("relay_token") or ""
                    if not tok or not hmac.compare_digest(self.headers.get("X-Ingest-Token", ""), tok):
                        return self._json({"ok": False, "error": "ongeldig token"}, 403)
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    info = release_store(self.rfile.read(n)) if 0 < n <= 60 * 1024 * 1024 else None
                    return self._json({"ok": bool(info), "release": info, "error": None if info else "geen geldig pakket"})
                if path0 in ("/tunnel/poll", "/tunnel/reply"):
                    tok = SETTINGS.get("relay_token") or ""
                    if not tok or not hmac.compare_digest(self.headers.get("X-Ingest-Token", ""), tok):
                        with ADMIN_FAIL_LOCK:
                            time.sleep(1.5)
                        return self._json({"ok": False, "error": "ongeldig token"}, 403)
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    if n > 90 * 1024 * 1024:
                        return self._json({"ok": False, "error": "te groot"}, 413)
                    try:
                        d = json.loads(self.rfile.read(n) or b"{}")
                    except ValueError:
                        return self._json({"ok": False, "error": "ongeldige JSON"}, 400)
                    if path0 == "/tunnel/reply":
                        TUNNEL.reply(str(d.get("id", "")), d)
                        return self._json({"ok": True})
                    name = str(d.get("name") or "").lower()
                    if not TUNNEL_NAME_RX.match(name):
                        return self._json({"ok": False, "error": "ongeldige naam"}, 400)
                    req = TUNNEL.poll(name, d.get("info"))
                    return self._json({"ok": True, "req": req})
                if path0.startswith("/admin/"):
                    return self._admin("POST")
                if path0 == "/api/cloud-update":
                    # beheerder van de cloudserver: zichzelf bijwerken naar de nieuwste GitHub-release
                    origin = self.headers.get("Origin") or ""
                    if not self._cloud_admin() or (origin and urllib.parse.urlsplit(origin).netloc != (self.headers.get("Host") or "")):
                        return self._json({"ok": False, "error": "beheerwachtwoord nodig"}, 403)
                    code, data, ctype = cloud_agent_call("POST", "/update/github", b"{}", {"X-Who": "beheer cloudserver"}, timeout=240)
                    return self._send(code, data, ctype)
                if path0 in ("/api/slides", "/api/slides/delete"):
                    origin = self.headers.get("Origin") or ""
                    if not self._cloud_admin() or (origin and urllib.parse.urlsplit(origin).netloc != (self.headers.get("Host") or "")):
                        return self._json({"ok": False, "error": "beheerwachtwoord nodig"}, 403)
                    return self._slides_post(path0)
                if path0 == "/api/info":
                    # beheer op de cloudserver zelf (zonder kastje); Origin moet deze site zijn
                    origin = self.headers.get("Origin") or ""
                    host = self.headers.get("Host") or ""
                    if not self._cloud_admin() or (origin and urllib.parse.urlsplit(origin).netloc != host):
                        return self._json({"ok": False, "error": "beheerwachtwoord nodig"}, 403)
                    try:
                        n = int(self.headers.get("Content-Length", 0) or 0)
                        new = info_set(json.loads(self.rfile.read(n) or b"{}"), "cloud")
                    except (ValueError, UnicodeDecodeError) as e:
                        return self._json({"ok": False, "error": str(e)}, 400)
                    hub.send("info", new)
                    return self._json({"ok": True, "info": new})
                if path0 == "/ingest":
                    tok = SETTINGS.get("relay_token") or ""
                    if not tok or not hmac.compare_digest(self.headers.get("X-Ingest-Token", ""), tok):
                        return self.send_error(403, "ongeldig token")
                    try:
                        n = int(self.headers.get("Content-Length", 0))
                        payload = json.loads(self.rfile.read(n))
                        items = payload.get("items") or []
                    except (ValueError, UnicodeDecodeError, AttributeError):
                        return self.send_error(400, "ongeldige JSON")
                    for it in items:
                        kind, data = it.get("t"), it.get("d")
                        if kind in ("state", "history", "clock", "callroom", "db"):
                            relay.ingest(kind, data)
                            if kind in ("state", "history", "clock"):
                                hub.send(kind, relay.state_json() if kind == "state" else data)
                    return self._json({"ok": True, "n": len(items)})
                if path0 == "/login":
                    n = int(self.headers.get("Content-Length", 0))
                    form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
                    pw_in = form.get("password", [""])[0]
                    nxt = self._safe_next(form.get("next", ["/jury"])[0])
                    pw = SETTINGS.get("jury_password") or ""
                    apw = SETTINGS.get("cloud_admin_password") or ""
                    if apw and hmac.compare_digest(pw_in, apw):      # beheerder: ook jurytoegang
                        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto", "").lower() == "https") else ""
                        self.send_response(303)
                        self.send_header("Set-Cookie", f"lt_a={session_token(apw + '|beheer')}; Path=/; Max-Age=43200; HttpOnly; SameSite=Strict{secure}")
                        self.send_header("Location", nxt)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        print("beheerder aangemeld op de cloudserver")
                        return
                    if pw and hmac.compare_digest(pw_in, pw):
                        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto", "").lower() == "https") else ""
                        self.send_response(303)
                        self.send_header("Set-Cookie", f"lt_s={session_token(pw)}; Path=/; Max-Age=43200; HttpOnly; SameSite=Lax{secure}")
                        self.send_header("Location", nxt)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    with LOGIN_FAIL_LOCK:   # foute pogingen één voor één: max ±0,7 per seconde
                        time.sleep(1.5)
                    return self._login_page(nxt, "Verkeerd wachtwoord")
                return self.send_error(403, "Wijzigen vanop afstand is nog niet mogelijk (gebeurt op de laptop)")
            if path0 in ("/api/programma", "/api/programma/clear"):
                if not self._is_admin():
                    return self._json({"ok": False, "error": "geen toegang (beheerderscode nodig)"}, 403) \
                        if "code" in self._json.__code__.co_varnames else self.send_error(403)
                if path0.endswith("/clear"):
                    prog_clear()
                    return self._json({"ok": True})
                n = int(self.headers.get("Content-Length", 0) or 0)
                if not 0 < n <= 40 * 1024 * 1024:
                    return self._json({"ok": False, "error": "bestand ontbreekt of is te groot (max 40 MB)"})
                data = self.rfile.read(n)
                fname = urllib.parse.unquote(self.headers.get("X-Filename", "") or "")
                try:
                    stats = prog_import(data, fname)
                except Exception as e:          # foutmelding naar de beheerpagina, server blijft draaien
                    PROG_STATUS["error"] = str(e)
                    return self._json({"ok": False, "error": str(e)})
                return self._json({"ok": True, "stats": stats, "file": PROG_STATUS["file"]})
            if path0 in ("/api/slides", "/api/slides/delete"):
                if not self._is_admin():
                    return self._json({"ok": False, "error": "beheerderscode nodig"}, 403)
                return self._slides_post(path0)
            if path0 == "/api/info":
                if not self._is_admin():
                    return self._json({"ok": False, "error": "beheerderscode nodig"}, 403)
                try:
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    new = info_set(json.loads(self.rfile.read(n) or b"{}"), "lokaal")
                except (ValueError, UnicodeDecodeError) as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                hub.send("info", new)
                cloud = None
                if SETTINGS.get("relay_url") and SETTINGS.get("relay_token"):
                    if new.get("mode") == "presentatie":
                        slides_push(new["deck"])                # eerst de dia's, dan pas het infoscherm
                    code, data, _ = cloud_call("POST", "info", json.dumps(new).encode(), timeout=15)
                    cloud = {"ok": code == 200, "error": None if code == 200 else (json.loads(data or b"{}").get("error") if data[:1] == b"{" else str(code))}
                return self._json({"ok": True, "info": new, "cloud": cloud})
            if path0.startswith("/api/sim/"):
                if not self._is_admin():
                    return self._json({"ok": False, "error": "beheerderscode nodig"}, 403)
                try:
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                    if not isinstance(body, dict):
                        raise ValueError
                except (ValueError, UnicodeDecodeError):
                    return self._json({"ok": False, "error": "ongeldige JSON"}, 400)
                if path0 == "/api/sim/start":
                    try:
                        ok, err = SIM.start(body)
                    except Exception as e:
                        ok, err = False, f"starten mislukt: {e}"
                    return self._json(dict(SIM.status(), ok=ok, error=err))
                if path0 == "/api/sim/stop":
                    SIM.stop()
                    return self._json(dict(SIM.status(), ok=True))
                if path0 == "/api/sim/cmd":
                    if not SIM.running():
                        return self._json({"ok": False, "error": "de simulator staat uit"}, 409)
                    return self._json({"ok": bool(SIM.eng.command(body))})
                return self.send_error(404)
            if path0 == "/api/db/sync":
                if not self._is_admin():
                    return self.send_error(403, "Geen toegang: enkel lokaal of met beheerderscode")
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n) or b"{}")
                except (ValueError, UnicodeDecodeError):
                    body = {}
                path = str(body.get("path") or SETTINGS["db_source_path"])
                if path != SETTINGS["db_source_path"]:
                    update_settings({"db_source_path": path})
                ok, err = db_refresh(path)
                return self._json(dict(db_status_json(), ok=ok))
            if self.path.split("?")[0] != "/api/settings":
                return self.send_error(404)
            if not self._is_admin():
                return self.send_error(403, "Geen toegang: enkel lokaal of met beheerderscode")
            try:
                n = int(self.headers.get("Content-Length", 0))
                new = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(new, dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                return self.send_error(400, "ongeldige JSON")
            errors = update_settings(new)
            if not errors and ("relay_url" in new or "relay_token" in new):
                apply_relay_settings()
            if errors:
                body = json.dumps({"ok": False, "errors": errors}, ensure_ascii=False).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            state.changed()
            return self._json({"ok": True})

        def _file(self, fn, ctype, cache="no-cache"):
            try:
                with open(fn, "rb") as f:
                    body = f.read()
            except OSError:
                return self.send_error(404)
            if ctype.startswith("text/html"):
                # versie in de scriptlink: Cloudflare laat browsers .js uren bewaren, zo komt een update meteen door
                body = body.replace(b'src="/infoscreen.js"', f'src="/infoscreen.js?v={VERSION}"'.encode())
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            view = self._view()
            q = hub.add(view)
            if STATS is not None:
                pg = urllib.parse.parse_qs(self.path.partition("?")[2]).get("page", [""])[0]
                STATS.open(q, "jury" if view == "jury" else ("callroom" if pg == "callroom" else "publiek"), STATS.visitor(self))
            try:
                now = now_fn()
                first = [("state", state.state_json(now)), ("history", state.history_json()),
                         ("clock", state.clock_json()), ("info", dict(INFO))]
                for ev, data in first:
                    self.wfile.write(sse_msg(ev, public_view(ev, data) if view == "publiek" else data))
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                    except queue.Empty:
                        msg = b": ping\n\n"
                    self.wfile.write(msg)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                hub.remove(q)
                if STATS is not None:
                    STATS.close(q)

    return H


# ---------- bronnen ----------
class Intake:
    """Eén actieve bron (socket of tshark) zodat pakketten niet dubbel binnenkomen."""
    def __init__(self, state, rawlog):
        self.state, self.rawlog = state, rawlog
        self.active = None
        self.lock = threading.Lock()

    SWITCH_AFTER_S = 5.0     # andere bron neemt over als de actieve zo lang stil is

    def feed(self, source, src_ip, data):
        with self.lock:
            now = time.time()
            real_wins = source == "socket" and self.active == "simulator" and SETTINGS.get("sim_stop_on_real", True)
            if self.active is None or real_wins or (source != self.active and now - getattr(self, "last_active", 0) > self.SWITCH_AFTER_S):
                prev = self.active
                self.active = source
                STATE_SOURCE.update(name=source, sim=(source == "simulator"))
                print(f"data ontvangen via {source} van {src_ip}" + (f" (was {prev})" if prev else ""))
                if prev == "simulator" and source != "simulator":
                    if SIM.running():
                        SIM.stop("echte gegevens van SwimTime ontvangen")
                    self.state.drop_simulated()
            if source != self.active:
                return
            self.last_active = now
            if data.startswith(b"#SIM"):
                if source == "simulator":
                    sim_control(data)
                return
            if self.rawlog:
                self.rawlog.write(f"{time.time():.3f}\t{src_ip}\t{data.hex()}\n")
            self.state.handle(data, now_fn())


UDP_STATUS = {"interface": "", "allow": "", "error": None, "lastFrom": None, "lastAt": None,
              "rejected": 0, "lastRejected": None, "seen": {}}
SWIMTIME_PREFIX = (b"Time	", b"Layout	", b"Meet	", b"Session	", b"Event	", b"Heat	", b"Competitor	", b"Record")
IP_PKTINFO = getattr(socket, "IP_PKTINFO", 8 if sys.platform.startswith("linux") else None)


def udp_seen(iface, src, data):
    """Automatische herkenning: waar (verbinding + afzender) komen de SwimTime-pakketten binnen?"""
    if not data.startswith(SWIMTIME_PREFIX):
        return
    k = f"{iface or '?'}|{src}"
    e = UDP_STATUS["seen"].get(k)
    if e is None:
        if len(UDP_STATUS["seen"]) > 20:
            return
        e = UDP_STATUS["seen"][k] = {"iface": iface or "", "from": src, "count": 0, "first": time.time()}
        print(f"SwimTime-broadcast gevonden: van {src}" + (f" via {iface}" if iface else ""))
    e["count"] += 1
    e["last"] = time.time()


def _allow_list(txt):
    nets = []
    for part in re.split(r"[,;\s]+", txt or ""):
        if part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                UDP_STATUS["error"] = f"ongeldige afzender: {part}"
    return nets


# ---------- elkaar vinden op hetzelfde netwerk (servers, schermkastjes, laptop) ----------
DISCO_PORT = 2627
PEERS = {}          # ip -> {name, role, version, port, seen}
DISCO_ID = _secrets.token_hex(6)


def discovery(http_port):
    """Elke 5 s een korte aankondiging (UDP-broadcast) en luisteren naar de anderen. Geen gevoelige gegevens."""
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        rx.bind(("", DISCO_PORT))
    except OSError as e:
        print(f"elkaar vinden: UDP/{DISCO_PORT} bezet ({e})")
        rx = None

    def listen():
        while rx is not None:
            try:
                data, addr = rx.recvfrom(1024)
                d = json.loads(data.decode("utf-8"))
                if d.get("khzs") != 1 or d.get("id") == DISCO_ID:
                    continue
                PEERS[addr[0]] = {"name": str(d.get("name", ""))[:60], "label": str(d.get("label", ""))[:60], "role": str(d.get("role", ""))[:20],
                                  "version": str(d.get("version", ""))[:20], "port": int(d.get("port") or 80),
                                  "live": bool(d.get("live")), "seen": time.time()}
            except (OSError, ValueError, UnicodeDecodeError, TypeError):
                time.sleep(0.2)
    threading.Thread(target=listen, daemon=True).start()
    while True:
        live = False
        if STATE is not None:
            try:
                live = bool(STATE.state_json(now_fn()).get("connected"))
            except Exception:
                pass
        msg = json.dumps({"khzs": 1, "id": DISCO_ID, "name": socket.gethostname(), "label": SETTINGS.get("device_label") or "",
                          "role": ROLE, "version": VERSION,
                          "port": http_port, "live": live}).encode()
        try:
            tx.sendto(msg, ("255.255.255.255", DISCO_PORT))
        except OSError:
            pass
        for ip, p in list(PEERS.items()):
            if time.time() - p["seen"] > 60:
                PEERS.pop(ip, None)
        time.sleep(5)


def peers_json():
    return [dict(p, ip=ip, url=f"http://{ip}" + ("" if p["port"] == 80 else f":{p['port']}"),
                 ago=round(time.time() - p["seen"])) for ip, p in sorted(PEERS.items())]


def udp_listener(intake, port):
    """UDP/26 van SwimTime. Optioneel enkel op één interface (SO_BINDTODEVICE) en/of van toegelaten afzenders;
    wijzigingen in de instellingen worden binnen een seconde toegepast (geen herstart nodig)."""
    s, cur_if, cur_allow, nets = None, None, None, []
    while True:
        want_if = (SETTINGS.get("udp_interface") or "").strip()
        want_allow = (SETTINGS.get("udp_allow_from") or "").strip()
        if s is None or want_if != cur_if:
            if s is not None:
                s.close()
            UDP_STATUS["error"] = None
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if want_if:
                if hasattr(socket, "SO_BINDTODEVICE"):
                    try:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, want_if.encode())
                    except OSError as e:
                        UDP_STATUS["error"] = f"interface {want_if}: {e}"
                else:
                    UDP_STATUS["error"] = "een interface kiezen kan enkel op het kastje (Linux); hier wordt op alle verbindingen geluisterd"
            try:
                s.bind(("", port))
            except OSError as e:
                UDP_STATUS["error"] = f"UDP/{port}: {e}"
                s.close()
                s = None
                time.sleep(5)
                continue
            s.settimeout(1.0)
            pktinfo = False
            if IP_PKTINFO is not None and hasattr(s, "recvmsg"):
                try:
                    s.setsockopt(socket.IPPROTO_IP, IP_PKTINFO, 1)
                    pktinfo = True
                except OSError:
                    pass
            cur_if = want_if
            UDP_STATUS["interface"] = want_if
            print(f"luistert op UDP/{port}" + (f" (enkel {want_if})" if want_if else " (alle verbindingen)"))
        if want_allow != cur_allow:
            cur_allow, nets = want_allow, _allow_list(want_allow)
            UDP_STATUS["allow"] = want_allow
        iface = want_if or None
        try:
            if pktinfo:
                data, anc, _, addr = s.recvmsg(4096, socket.CMSG_SPACE(12))
                for lvl, typ, cd in anc:
                    if lvl == socket.IPPROTO_IP and typ == IP_PKTINFO and len(cd) >= 4:
                        try:
                            iface = socket.if_indextoname(struct.unpack("i", cd[:4])[0])
                        except OSError:
                            pass
            else:
                data, addr = s.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            s = None
            time.sleep(1)
            continue
        if nets:
            ip = ipaddress.ip_address(addr[0])
            if not any(ip in n for n in nets):
                UDP_STATUS["rejected"] += 1
                UDP_STATUS["lastRejected"] = addr[0]
                continue
        UDP_STATUS["lastFrom"], UDP_STATUS["lastAt"] = addr[0], time.time()
        udp_seen(iface, addr[0], data)
        intake.feed("socket", addr[0], data)


def sim_control(data):
    """Stuurregels van de simulator: '#SIMPROGRAM <json>' (programma) en '#SIMFINISHED <event> <heat>'."""
    try:
        cmd, _, rest = data.decode("utf-8").strip().partition(" ")
        if cmd == "#SIMPROGRAM":
            prog = json.loads(rest)
            if isinstance(prog, list):
                SIM_SCHEDULE[:] = prog
                print(f"simulatieprogramma ontvangen: {len(prog)} reeksen")
        elif cmd == "#SIMFINISHED":
            ev, ht = rest.split()[:2]
            for h in SIM_SCHEDULE:
                if h["event"]["number"] == ev and h["heat"]["number"] == ht:
                    h["finished"] = True
    except (ValueError, KeyError, UnicodeDecodeError) as e:
        print("simulator-stuurregel genegeerd:", e)


class SimHost:
    """Ingebouwde simulator: zelfde engine als simulator.py, maar in de server (start/stop via het beheer)."""
    def __init__(self):
        self.eng = None
        self.lock = threading.Lock()
        self.started = None
        self.last_stop = None

    def available(self):
        if INTAKE is None:
            return "de simulator werkt enkel als de live timing draait (rol: server)"
        return None

    def running(self):
        return self.eng is not None and not self.eng.quit

    def _deliver(self, msg):
        if INTAKE is not None:
            INTAKE.feed("simulator", "ingebouwd", msg.encode("utf-8") + b"\r")

    def options(self, over=None):
        o = {k[4:]: SETTINGS[k] for k in DEFAULT_SETTINGS if k.startswith("sim_") and k not in ("sim_autostart", "sim_stop_on_real")}
        for k, v in (over or {}).items():
            if k in o and v is not None:
                o[k] = type(DEFAULT_SETTINGS["sim_" + k])(v)
        o["seed"] = o["seed"] or None
        o["events"] = max(1, min(60, int(o["events"])))
        o["per_event"] = max(1, min(80, int(o["per_event"])))
        o["lanes"] = max(4, min(10, int(o["lanes"])))
        o["speed"] = max(0.25, min(64.0, float(o["speed"])))
        return o

    STATE_FILE = None

    def _remember(self, running, over=None):
        """Lopende simulator onthouden: na een herstart van de server (rol, update, stroom) loopt hij gewoon verder."""
        try:
            with open(os.path.join(DATA, "simulator.json"), "w", encoding="utf-8") as f:
                json.dump({"running": running, "options": over or {}}, f)
        except OSError:
            pass

    def resume(self):
        try:
            d = json.load(open(os.path.join(DATA, "simulator.json"), encoding="utf-8"))
        except (OSError, ValueError):
            return
        if d.get("running"):
            print("simulator liep voor de herstart: opnieuw gestart")
            self.start(d.get("options") or {})

    def start(self, over=None):
        err = self.available()
        if err:
            return False, err
        sys.path.insert(0, BASE)
        import simulator
        with self.lock:
            self.stop(None)
            o = self.options(over)
            ns = argparse.Namespace(host="ingebouwd", port=0, udp=None, allow_broadcast=False, direct=self._deliver, **o)
            eng = simulator.Engine(ns)
            eng.tx.send(eng.program_msg())
            for fn in (eng.heartbeat, eng.loop):
                threading.Thread(target=fn, daemon=True).start()
            self.eng, self.started, self.opts = eng, time.time(), o
            self._remember(True, over)
            print(f"ingebouwde simulator gestart: {len(eng.prog.events)} wedstrijden, {len(eng.order)} reeksen, x{eng.speed:g}")
        return True, None

    def stop(self, why="gestopt via het beheer"):
        eng, self.eng = self.eng, None
        if eng is not None:
            eng.quit = True
            self.last_stop = {"at": time.time(), "why": why}
            if why:                                  # bewust gestopt (beheer of echte gegevens): niet hervatten
                self._remember(False)
            if why:
                print("ingebouwde simulator gestopt:", why)

    def status(self):
        d = {"running": self.running(), "available": self.available() is None, "reason": self.available(),
             "startedAt": self.started if self.running() else None, "lastStop": self.last_stop,
             "options": self.opts if self.running() and getattr(self, "opts", None) else self.options()}
        return d


SIM = SimHost()
INTAKE = None


def feed_listener(intake, bind, port):
    """TCP-invoer naast UDP/26: één bericht per regel (zelfde inhoud als een SwimTime-pakket), afgesloten met LF."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((bind, port))
    except OSError as e:
        print(f"simulator-invoer TCP/{port} niet beschikbaar: {e}")
        return
    srv.listen(4)
    print(f"simulator-invoer op tcp://{bind}:{port}")

    def client(conn, addr):
        buf = b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line:
                        intake.feed("simulator", addr[0], line)
        print(f"simulator {addr[0]} losgekoppeld")

    while True:
        conn, addr = srv.accept()
        print(f"simulator verbonden van {addr[0]}")
        threading.Thread(target=client, args=(conn, addr), daemon=True).start()


def tshark_interfaces():
    out = subprocess.run([TSHARK, "-D"], capture_output=True, text=True).stdout
    skip = ("loopback", "etwdump", "bluetooth", "ciscodump", "randpkt", "sshdump", "udpdump", "wifidump")
    ifaces = []
    for line in out.splitlines():
        if any(x in line.lower() for x in skip) or ". " not in line:
            continue
        ifaces.append(line.split(". ", 1)[1].split(" (")[0])
    return ifaces


def tshark_listener(intake, port, iface=None):
    """Leest pakketten via Npcap (vóór de Windows Firewall)."""
    ifaces = tshark_interfaces() if (not iface or iface.lower() == "all") else [iface]
    cmd = [TSHARK, "-l", "-n", "-Q", "-p"]   # -p: niet-promiscue, enkel eigen verkeer + broadcast
    for i in ifaces:
        cmd += ["-i", i]
    cmd += ["-f", f"udp dst port {port}", "-T", "fields", "-e", "ip.src", "-e", "data.data"]
    print(f"tshark-capture gestart op {len(ifaces)} interface(s)")
    while True:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in proc.stdout:
            ip, _, hx = line.rstrip("\n").partition("\t")
            if hx:
                try:
                    intake.feed("tshark", ip, bytes.fromhex(hx.replace(":", "")))
                except ValueError:
                    pass
        print("tshark gestopt, herstart over 3 s")
        time.sleep(3)


def fallback_watchdog(intake, port, iface, after=8.0):
    time.sleep(after)
    if intake.active is None:
        if os.path.exists(TSHARK):
            print(f"geen data via de socket na {after:.0f} s (firewall?) -> overschakelen op tshark/Npcap")
            tshark_listener(intake, port, iface)
        else:
            print("geen data via de socket en tshark niet gevonden: controleer de firewall (UDP/26 inkomend)")


def replay(state, path, speed):
    """Speel een pcap(ng) (via tshark) of een raw.log opnieuw af."""
    if path.endswith((".pcap", ".pcapng")):
        out = subprocess.run([TSHARK, "-r", path, "-Y", "udp.dstport==26", "-T", "fields",
                              "-e", "frame.time_epoch", "-e", "data.data"], capture_output=True, text=True).stdout
        rows = [(float(a), b.replace(":", "")) for a, b in (l.split("\t") for l in out.splitlines() if "\t" in l) if b]
    else:
        rows = []
        with open(path) as f:
            for l in f:
                p = l.rstrip("\n").split("\t")
                if len(p) == 3:
                    rows.append((float(p[0]), p[2]))
    print(f"replay {len(rows)} pakketten aan {speed}x")
    if not rows:
        return
    start_virt, start_cap = now_fn(), rows[0][0]
    for ts, hx in rows:
        target = start_virt + (ts - start_cap)
        d = (target - now_fn()) / speed
        if d > 0:
            time.sleep(d)
        state.handle(bytes.fromhex(hx), target)
    print("replay klaar")


PUSHER = None


def apply_relay_settings():
    """(Her)start de zender volgens settings.json; leeg relay_url = uit."""
    global PUSHER
    if PUSHER:
        PUSHER.stopped = True
        PUSHER.ev.set()
        PUSHER = None
    url = (SETTINGS.get("relay_url") or "").strip()
    if url:
        PUSHER = RelayPusher(url, SETTINGS.get("relay_token") or "")
        print(f"zend-modus: stuurt naar {url}")
    else:
        print("zend-modus uit (geen relay_url)")


def pump(state, hub):
    """Stuurt clock ~10x/s en state/history bij wijziging (max 5x/s); zend-modus: ook naar de relay."""
    sent_v = sent_h = -1
    last_clock = None
    last_state = last_aux = last_hist = 0.0
    last_hk = None
    while True:
        time.sleep(0.1)
        now = now_fn()
        state.tick(now)
        c = state.clock_json()
        if c != last_clock or c["status"] == "running":
            hub.send("clock", c)
            if PUSHER:
                PUSHER.send("clock", c)
            last_clock = c
        if (state.version != sent_v and now - last_state >= 0.2) or now - last_state >= 2:
            sent_v = state.version
            last_state = now
            st = state.state_json(now)
            hub.send("state", st)
            if PUSHER:
                PUSHER.send("state", st)
        if state.history_version != sent_h:
            sent_h = state.history_version
            h = state.history_json()
            hub.send("history", h)
            if PUSHER:
                PUSHER.send("history", h)
        if PUSHER and now - last_hist >= 30:
            last_hist = now
            PUSHER.send("history", state.history_json())
        hk = (str((st_cur := state.cur) and st_cur.event.get("EventName")), str(st_cur and st_cur.heat.get("HeatName")))
        if PUSHER and (now - last_aux >= 5 or hk != last_hk):     # nieuwe reeks: oproepkamer meteen doorsturen
            last_aux, last_hk = now, hk
            try:
                PUSHER.send("callroom", callroom_json(public_view("state", state.state_json(now))))
                PUSHER.send("db", db_status_json())
            except Exception as e:      # mag de pomp nooit stoppen
                print("relay aux:", e)


def main():
    global _SPEED, INTAKE
    ap = argparse.ArgumentParser(description="ALGE SwimTime read-only live timing")
    ap.add_argument("--udp-port", type=int, default=26)
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--replay", help="pcap/pcapng of raw.log opnieuw afspelen i.p.v. live luisteren")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--settings", default=os.path.join(DATA, "settings.json"))
    ap.add_argument("--history", default=os.path.join(DATA, "history.json"))
    ap.add_argument("--selftest", action="store_true", help="enkel controleren of de code laadt en antwoordt (voor updates)")
    ap.add_argument("--version", action="version", version=VERSION)
    ap.add_argument("--no-rawlog", action="store_true")
    ap.add_argument("--source", choices=("auto", "socket", "tshark"), default="socket",
                    help="socket (standaard) = enkel een UDP-socket; tshark/auto gebruiken Npcap en kunnen "
                         "EDR/XDR-meldingen veroorzaken (bv. Cisco XDR) - enkel na overleg met security")
    ap.add_argument("--iface", default="Wi-Fi", help="tshark-interface (naam zoals in Windows, bv. Wi-Fi of Ethernet; 'all' = alle)")
    ap.add_argument("--open", action="store_true", help="browser openen bij start")
    ap.add_argument("--feed-port", type=int, help="TCP-poort voor de simulator (standaard uit settings: 2626; 0 = uit)")
    ap.add_argument("--relay", help="zend-modus: URL van de publieke server (bv. https://live.club.be)")
    ap.add_argument("--relay-token", help="gedeeld geheim met de publieke server (anders uit settings.json)")
    ap.add_argument("--relay-server", action="store_true", help="relay-modus: publieke server die van de laptop ontvangt")
    ap.add_argument("--jury-password", help="relay-modus: wachtwoord voor /jury en /callroom (anders uit settings.json)")
    args = ap.parse_args()
    load_settings(args.settings)
    if args.selftest:                 # updater: laadt de code, instellingen en historiek zonder iets te openen
        import tempfile
        h = os.path.join(tempfile.mkdtemp(), "history.json")
        if os.path.exists(args.history):
            with open(args.history, "rb") as fi, open(h, "wb") as fo:
                fo.write(fi.read())
        st = State(h)
        json.dumps(st.state_json(now_fn()))
        json.dumps(st.history_json())
        json.dumps(callroom_json(public_view("state", st.state_json(now_fn()))))
        for f in ("index.html", "callroom.html", "settings.html"):
            assert os.path.getsize(os.path.join(STATIC, f)) > 1000, f
        print(f"selftest OK {VERSION}")
        return

    global STATE, PUSHER, STATS
    if args.relay_token:
        SETTINGS["relay_token"] = args.relay_token
    if args.jury_password:
        SETTINGS["jury_password"] = args.jury_password
    if args.relay:
        SETTINGS["relay_url"] = args.relay
    global IS_RELAY
    IS_RELAY = bool(args.relay_server)
    if args.relay_server:
        relay = RelayStore(os.path.join(os.path.dirname(os.path.abspath(args.settings)), "relay_cache.json"))
        STATS = Stats(os.path.join(os.path.dirname(os.path.abspath(args.settings)), "stats.json"))
        info_load(os.path.join(os.path.dirname(os.path.abspath(args.settings)), "info.json"))
        hub = Hub()
        threading.Thread(target=relay_watchdog, args=(relay, hub), daemon=True).start()
        if not SETTINGS.get("relay_token"):
            print("WAARSCHUWING: geen relay_token ingesteld; /ingest weigert alles")
        if not SETTINGS.get("jury_password"):
            print("WAARSCHUWING: geen jury_password ingesteld; /jury en /callroom zijn niet bereikbaar")
        srv = ThreadingHTTPServer((args.bind, args.http_port), make_handler(relay, hub, relay))
        srv.daemon_threads = True
        print(f"relay-modus: luistert op http://{args.bind}:{args.http_port}/  (ingest: POST /ingest)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    state = State(args.history)
    STATE = state
    STATS = Stats(os.path.join(DATA, "stats.json"))
    info_load(os.path.join(DATA, "info.json"))
    if ROLE != "server":
        print(f"rol '{ROLE}': live timing staat uit (enkel beheer en scherm)")
    elif SETTINGS.get("relay_url"):
        apply_relay_settings()
    prog_load()
    if not args.replay and ROLE == "server":
        threading.Thread(target=db_load_latest_local, daemon=True).start()
        threading.Thread(target=db_interval_loop, daemon=True).start()
    hub = Hub()
    if args.replay:
        _SPEED = args.speed
        threading.Thread(target=replay, args=(state, args.replay, args.speed), daemon=True).start()
    elif ROLE == "server":
        rawlog = None
        if not args.no_rawlog:
            os.makedirs(os.path.join(DATA, "logs"), exist_ok=True)
            rawlog = open(os.path.join(DATA, "logs", datetime.now().strftime("raw_%Y%m%d_%H%M%S.log")), "a", buffering=1)
        intake = Intake(state, rawlog)
        INTAKE = intake
        if SETTINGS.get("sim_autostart"):
            threading.Timer(3.0, SIM.start).start()
        else:
            threading.Timer(3.0, SIM.resume).start()
        fport = args.feed_port if args.feed_port is not None else int(SETTINGS.get("feed_port") or 0)
        if fport:
            threading.Thread(target=feed_listener, args=(intake, SETTINGS.get("feed_bind") or "127.0.0.1", fport),
                             daemon=True).start()
        if args.source in ("auto", "socket"):
            threading.Thread(target=udp_listener, args=(intake, args.udp_port), daemon=True).start()
        if args.source == "auto":
            threading.Thread(target=fallback_watchdog, args=(intake, args.udp_port, args.iface), daemon=True).start()
        elif args.source == "tshark":
            threading.Thread(target=tshark_listener, args=(intake, args.udp_port, args.iface), daemon=True).start()
    threading.Thread(target=pump, args=(state, hub), daemon=True).start()
    if AGENT_URL:
        threading.Thread(target=agent_poll, daemon=True).start()
    tunnel_client(args.http_port)
    threading.Thread(target=discovery, args=(args.http_port,), daemon=True).start()

    https_server(make_handler(state, hub), args.bind)
    srv = ThreadingHTTPServer((args.bind, args.http_port), make_handler(state, hub))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://localhost:{args.http_port}/"
    print(f"\nLive timing draait op {url}")
    for ip in lan_ips():
        print(f"  op het netwerk: http://{ip}:{args.http_port}/")
    print("\nToetsen: [o] publieke pagina   [j] jury   [s] instellingen   [q] stoppen\n")
    if args.open:
        webbrowser.open(url + "jury")
    try:
        key_loop(url)
    except KeyboardInterrupt:
        pass
    print("gestopt")


def lan_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.")):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def key_loop(url):
    try:
        import msvcrt
    except ImportError:          # niet-Windows: enkel Ctrl+C
        while True:
            time.sleep(1)
    while True:
        if msvcrt.kbhit():
            k = msvcrt.getwch().lower()
            if k == "o":
                webbrowser.open(url + "publiek")
            elif k == "j":
                webbrowser.open(url + "jury")
            elif k == "s":
                webbrowser.open(url + "settings")
            elif k == "q":
                return
        time.sleep(0.1)


if __name__ == "__main__":
    main()
