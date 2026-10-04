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
import json
import os
import re
import shutil
import queue
import socket
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
TSHARK = r"C:\Program Files\Wireshark\tshark.exe"

# Alle drempels/opties staan in settings.json en zijn aanpasbaar via /settings.
# Waarden in seconden tenzij anders vermeld.
DEFAULT_SETTINGS = {
    "manual_threshold_s": 1.5,     # tijd komt later binnen dan start + tijd = manueel ingegeven (handtijd)
    "clear_grace_s": 3.0,          # LaneTime -1 pas toepassen als er geen nieuwe waarde volgt
    "stale_after_s": 10.0,         # zonder pakketten = geen verbinding met tijdsysteem
    "history_max": 200,            # aantal bewaarde reeksen
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
    "splash_seconds": 2.0,         # duur van het tussenscherm
    "splash_jury": True,           # ook op de jurypagina tonen
    "splash_callroom": True,       # ook in de oproepkamer tonen
    "callroom_heats": 4,           # oproepkamer: aantal aankomende reeksen links
    "callroom_next_big": True,     # oproepkamer: eerstvolgende reeks groter bovenaan
    "callroom_next_scale": 1.6,    # oproepkamer: vergroting van de eerstvolgende reeks
    "callroom_show_previous": False,  # oproepkamer: vorige reeks rechts tonen (onder de huidige)
    "callroom_split_left_pct": 60, # oproepkamer: breedte linkerkant (aankomende reeksen) in %
    "db_source_path": r"W:\2026_PK.mdb",  # SwimTime-database; wordt ENKEL gekopieerd op commando (Synchroniseren)
    "db_auto_after_heat": True,    # na elke reeks automatisch een kopie nemen
    "db_after_heat_delay_s": 10.0, # wachttijd na het laden van de volgende startlijst
    "db_interval_s": 0.0,          # >0: elke X s een kopie als het bestand gewijzigd is (0 = uit) = "Live database"
    "db_live_interval_s": 1.0,     # live database: elke X s controleren of het bestand gewijzigd is
    "relay_min_takeover_s": -0.03, # aflossing: overnametijd lager dan dit = te vroege wissel
    "max_peer_panel_diff_s": 0.30, # jury: verschil peer/paneel groter dan dit = markeren
    "show_peer": True,             # jury: handtijd (peer) klein onder de paneeltijd
    "admin_token": "",             # beheerderscode om instellingen te wijzigen buiten localhost
}
SETTINGS = dict(DEFAULT_SETTINGS)
SETTINGS_FILE = os.path.join(BASE, "settings.json")
PUBLIC_SETTINGS = ("splash_enabled", "splash_seconds", "splash_jury", "splash_callroom", "public_split_layout", "jury_split_left_pct", "callroom_heats", "callroom_next_big", "callroom_next_scale", "callroom_show_previous", "callroom_split_left_pct", "show_reaction", "show_manual", "show_suspect", "show_clock", "overview_heats", "show_peer", "max_peer_panel_diff_s")


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
    db = DB_DATA.get((str(ev.get("number", "")), heat_nr))
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


class Heat:
    def __init__(self, event, heat):
        self.event = dict(event)
        self.heat = dict(heat)
        self.lanes = {}
        self.t0 = None            # monotonic tijdstip van de start (geschat uit RunningTime)
        self.started = False

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
            db_after_heat()
        self.cur = Heat(self.event, self.heat_info)

    def _archive(self, h):
        item = {"event": self._event_json(h.event), "heat": self._heat_json(h.heat, h.event.get("Discipline", "")),
                "finishedAt": datetime.now().strftime("%H:%M"), "lanes": self._lanes_json(h)}
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
        e = enrich_heat({"event": self._event_json(h.event), "heat": self._heat_json(h.heat, h.event.get("Discipline", "")), "lanes": self._lanes_json(h)})
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
            meet = {"name": self.meet.get("MeetName", ""), "city": self.meet.get("City", ""),
                    "date": alge_date(self.meet.get("Date")),
                    "session": self.session.get("SessionNumber", ""),
                    "sessionName": self.session.get("SessionName", "")}
            no_heat = not h or (not h.event.get("EventName") and not any(l.has_content() for l in h.lanes.values()))
            if no_heat and self.history:
                # nog geen (volledige) reeks ontvangen, bv. na herstart: laatste gekende reeks tonen
                last = enrich_heat(dict(self.history[0]))
                return {
                    "connected": ago is not None and ago < SETTINGS["stale_after_s"],
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
                "connected": ago is not None and ago < SETTINGS["stale_after_s"],
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


DB_DIR = os.path.join(BASE, "db")
DB_EXPORT = os.path.join(BASE, "tools", "db_export.ps1")
DB_STATUS = {"lastSync": None, "file": None, "size": None, "sourceMtime": None, "error": None, "busy": False,
             "loadedAt": None, "loadedFile": None, "heats": 0}
DB_LOCK = threading.Lock()
DB_DATA = {}          # (eventNr, heatNr) -> {"start": t, "lanes": {lane: {"reaction": t, "laps": {lap: {...}}}}}
DB_SCHEDULE = []      # alle reeksen van de wedstrijd in programmavolgorde (voor de oproepkamer)
STATE = None          # gezet in main(), om na een import de clients te verversen


def db_copy(path):
    """Eén kopie van de SwimTime-database naar db/. De bron wordt enkel lezend geopend en anderen
    mogen blijven lezen/schrijven (geen Access-locks, geen .ldb). Geeft het pad van de kopie terug.
    Een relatief pad (bv. ..\2026_PK.mdb) geldt t.o.v. de map van de toepassing."""
    if path and not os.path.isabs(path):
        path = os.path.normpath(os.path.join(BASE, path))
    if not path or not os.path.isfile(path):
        raise OSError(f"bestand niet gevonden: {path} (is de netwerkschijf verbonden?)")
    os.makedirs(DB_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base, ext = os.path.splitext(os.path.basename(path))
    dst = os.path.join(DB_DIR, f"{base}_{stamp}{ext}")
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
                     size=os.path.getsize(dst), sourceMtime=datetime.fromtimestamp(src_mtime).strftime("%H:%M:%S"))
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


def db_refresh(path=None, reason="manueel"):
    """Kopie nemen + inlezen. Een kopie die niet leesbaar is (SwimTime schreef net) wordt weggegooid."""
    path = path or SETTINGS["db_source_path"]
    with DB_LOCK:
        if DB_STATUS["busy"]:
            return False, "er loopt al een synchronisatie"
        DB_STATUS["busy"] = True
    try:
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
        if src and not os.path.isabs(src):
            src = os.path.normpath(os.path.join(BASE, src))
        try:
            m = os.path.getmtime(src)
        except OSError:
            continue
        if m == last_mtime:
            continue
        # wachten tot het bestand 1 s niet meer verandert (SwimTime schrijft in meerdere stappen), max 5 s
        deadline = time.time() + 5
        while time.time() < deadline:
            time.sleep(1)
            try:
                m2 = os.path.getmtime(src)
            except OSError:
                break
            if m2 == m:
                break
            m = m2
        ok, _ = db_refresh(reason="live")
        if ok:
            last_mtime = m


def db_load_latest_local():
    """Bij opstart: de nieuwste lokale kopie inlezen (geen netwerkverkeer)."""
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


def callroom_json(state_now):
    """Volgende N reeksen na de huidige live reeks, uit het programma van de databasekopie."""
    n = max(1, int(SETTINGS["callroom_heats"]))
    ev = str((state_now.get("event") or {}).get("number") or "")
    m = re.search(r"(\d+)\s*$", (state_now.get("heat") or {}).get("name") or "")
    ht = m.group(1) if m else str((state_now.get("heat") or {}).get("number") or "")
    pos = next((i for i, h in enumerate(DB_SCHEDULE) if h["event"]["number"] == ev and h["heat"]["number"] == ht), None)
    if pos is None:
        today = datetime.now().strftime("%Y-%m-%d")
        pos = next((i - 1 for i, h in enumerate(DB_SCHEDULE) if not h["finished"] and h["date"] >= today), len(DB_SCHEDULE))
    # volgende, nog niet afgewerkte reeksen; ook zonder startlijst (lanes leeg / lanesKnown false)
    upcoming = [h for h in DB_SCHEDULE[pos + 1:] if not h["finished"]][:n]
    return {"upcoming": upcoming, "count": n, "matched": pos is not None and 0 <= pos < len(DB_SCHEDULE),
            "db": {"loadedAt": DB_STATUS["loadedAt"], "loadedFile": DB_STATUS["loadedFile"], "heats": len(DB_SCHEDULE)}}


def db_status_json():
    return {k: DB_STATUS[k] for k in ("lastSync", "sourceMtime", "error", "busy", "loadedAt", "loadedFile", "heats")} | {
        "source": SETTINGS["db_source_path"], "autoAfterHeat": SETTINGS["db_auto_after_heat"],
        "intervalS": SETTINGS["db_interval_s"], "liveIntervalS": SETTINGS["db_live_interval_s"],
        "live": float(SETTINGS["db_interval_s"] or 0) > 0}


def make_handler(state, hub):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _view(self):
            q = self.path.partition("?")[2]
            return "jury" if "view=jury" in q else "publiek"

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/state":
                d = state.state_json(now_fn())
                return self._json(public_view("state", d) if self._view() == "publiek" else d)
            if path == "/history":
                d = state.history_json()
                return self._json(public_view("history", d) if self._view() == "publiek" else d)
            if path == "/events":
                return self._sse()
            if path in ("/", "/index.html"):
                self.send_response(302)
                self.send_header("Location", "/publiek")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path in ("/publiek", "/jury"):
                return self._file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
            if path in ("/settings", "/settings.html"):
                return self._file(os.path.join(STATIC, "settings.html"), "text/html; charset=utf-8")
            if path == "/api/callroom":
                st = public_view("state", state.state_json(now_fn()))
                return self._json(callroom_json(st))
            if path in ("/callroom", "/callroom.html"):
                return self._file(os.path.join(STATIC, "callroom.html"), "text/html; charset=utf-8")
            if path == "/api/db":
                return self._json(dict(db_status_json(), canEdit=self._is_admin()))
            if path == "/api/settings":
                return self._json({"settings": {k: v for k, v in SETTINGS.items() if k != "admin_token"},
                                   "defaults": {k: v for k, v in DEFAULT_SETTINGS.items() if k != "admin_token"},
                                   "canEdit": self._is_admin(), "tokenSet": bool(SETTINGS["admin_token"])})
            self.send_error(404)

        def _is_admin(self):
            # via cloudflared/proxy komt alles van localhost binnen: dan enkel met token
            proxied = any(self.headers.get(h) for h in ("Cf-Connecting-Ip", "X-Forwarded-For", "Cf-Ray"))
            local = self.client_address[0] in ("127.0.0.1", "::1") and not proxied
            tok = SETTINGS["admin_token"]
            return local or (bool(tok) and self.headers.get("X-Admin-Token") == tok)

        def do_POST(self):
            if self.path.split("?")[0] == "/api/db/sync":
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

        def _file(self, fn, ctype):
            try:
                with open(fn, "rb") as f:
                    body = f.read()
            except OSError:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
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
            try:
                now = now_fn()
                first = [("state", state.state_json(now)), ("history", state.history_json()),
                         ("clock", state.clock_json())]
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

    return H


# ---------- bronnen ----------
class Intake:
    """Eén actieve bron (socket of tshark) zodat pakketten niet dubbel binnenkomen."""
    def __init__(self, state, rawlog):
        self.state, self.rawlog = state, rawlog
        self.active = None
        self.lock = threading.Lock()

    def feed(self, source, src_ip, data):
        with self.lock:
            if self.active is None:
                self.active = source
                print(f"data ontvangen via {source} van {src_ip}")
            if source != self.active:
                return
            if self.rawlog:
                self.rawlog.write(f"{time.time():.3f}\t{src_ip}\t{data.hex()}\n")
            self.state.handle(data, now_fn())


def udp_listener(intake, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", port))
    print(f"luistert op UDP/{port}")
    while True:
        data, addr = s.recvfrom(4096)
        intake.feed("socket", addr[0], data)


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


def pump(state, hub):
    """Stuurt clock ~10x/s en state/history bij wijziging (max 5x/s)."""
    sent_v = sent_h = -1
    last_clock = None
    last_state = 0.0
    while True:
        time.sleep(0.1)
        now = now_fn()
        state.tick(now)
        c = state.clock_json()
        if c != last_clock or c["status"] == "running":
            hub.send("clock", c)
            last_clock = c
        if (state.version != sent_v and now - last_state >= 0.2) or now - last_state >= 2:
            sent_v = state.version
            last_state = now
            hub.send("state", state.state_json(now))
        if state.history_version != sent_h:
            sent_h = state.history_version
            hub.send("history", state.history_json())


def main():
    global _SPEED
    ap = argparse.ArgumentParser(description="ALGE SwimTime read-only live timing")
    ap.add_argument("--udp-port", type=int, default=26)
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--replay", help="pcap/pcapng of raw.log opnieuw afspelen i.p.v. live luisteren")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--settings", default=os.path.join(BASE, "settings.json"))
    ap.add_argument("--history", default=os.path.join(BASE, "history.json"))
    ap.add_argument("--no-rawlog", action="store_true")
    ap.add_argument("--source", choices=("auto", "socket", "tshark"), default="socket",
                    help="socket (standaard) = enkel een UDP-socket; tshark/auto gebruiken Npcap en kunnen "
                         "EDR/XDR-meldingen veroorzaken (bv. Cisco XDR) - enkel na overleg met security")
    ap.add_argument("--iface", default="Wi-Fi", help="tshark-interface (naam zoals in Windows, bv. Wi-Fi of Ethernet; 'all' = alle)")
    ap.add_argument("--open", action="store_true", help="browser openen bij start")
    args = ap.parse_args()
    load_settings(args.settings)

    global STATE
    state = State(args.history)
    STATE = state
    if not args.replay:
        threading.Thread(target=db_load_latest_local, daemon=True).start()
        threading.Thread(target=db_interval_loop, daemon=True).start()
    hub = Hub()
    if args.replay:
        _SPEED = args.speed
        threading.Thread(target=replay, args=(state, args.replay, args.speed), daemon=True).start()
    else:
        rawlog = None
        if not args.no_rawlog:
            os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
            rawlog = open(os.path.join(BASE, "logs", datetime.now().strftime("raw_%Y%m%d_%H%M%S.log")), "a", buffering=1)
        intake = Intake(state, rawlog)
        if args.source in ("auto", "socket"):
            threading.Thread(target=udp_listener, args=(intake, args.udp_port), daemon=True).start()
        if args.source == "auto":
            threading.Thread(target=fallback_watchdog, args=(intake, args.udp_port, args.iface), daemon=True).start()
        elif args.source == "tshark":
            threading.Thread(target=tshark_listener, args=(intake, args.udp_port, args.iface), daemon=True).start()
    threading.Thread(target=pump, args=(state, hub), daemon=True).start()

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
