"""Programma (wedstrijden, reeksen, baanindeling, inschrijftijden) uit de wedstrijddatabank van Splash Meet Manager (.mdb).

Betrouwbaarder dan de binaire backup (.smb): echte kolommen, elke Meet Manager-versie. Leest enkel een LOKALE KOPIE en
enkel de kolommen hieronder (geen geboortedata, adressen of andere persoonsgegevens buiten naam en club).
Windows: Access-driver (tools/mdb_tables.ps1) · Linux/kastje: mdbtools.

Test:  python tools/mm_mdb.py kopie.mdb
"""
import json
import os
import subprocess
import sys
from collections import Counter

STROKES = {1: "Vrije slag", 2: "Rugslag", 3: "Schoolslag", 4: "Vlinderslag", 5: "Wisselslag"}
NT = 2147483647                                   # "geen tijd" in Meet Manager

COLS = {
    "SWIMSESSION": ["SWIMSESSIONID", "SESSIONNUMBER", "STARTDATE"],
    "SWIMSTYLE": ["SWIMSTYLEID", "DISTANCE", "RELAYCOUNT", "STROKE", "NAME"],
    "SWIMEVENT": ["SWIMEVENTID", "SWIMSESSIONID", "EVENTNUMBER", "SWIMSTYLEID", "GENDER", "DAYTIME"],
    "HEAT": ["HEATID", "SWIMEVENTID", "HEATNUMBER", "DAYTIME"],
    "CLUB": ["CLUBID", "CODE", "SHORTNAME"],
    "ATHLETE": ["ATHLETEID", "CLUBID", "FIRSTNAME", "LASTNAME", "GENDER"],
    "SWIMRESULT": ["ATHLETEID", "HEATID", "LANE", "ENTRYTIME"],
    "RELAY": ["RELAYID", "CLUBID", "NAME", "TEAMNUMBER", "HEATID", "LANE", "ENTRYTIME"],
}


def read_tables(path):
    if os.name == "nt":
        spec = ";".join(f"{t}:{','.join(c)}" for t, c in COLS.items())
        ps1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mdb_tables.ps1")
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1, "-File", path,
                            "-Spec", spec], capture_output=True, timeout=180)
        if r.returncode != 0:
            raise OSError("Access-databank lezen mislukt: " + r.stderr.decode("utf-8", "replace").strip()[-300:])
        d = json.loads(r.stdout.decode("utf-8-sig") or "{}")
        return {t: (rows if isinstance(rows, list) else [rows]) for t, rows in d.items()}
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db_mdbtools
    out = {}
    for t, cols in COLS.items():
        try:
            rows = db_mdbtools.table(path, t)
        except OSError:
            rows = []
        out[t] = [{c: r.get(c) for c in cols} for r in rows]          # enkel de gekozen kolommen bewaren
    return out


def _i(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _hhmm(v):
    v = str(v or "")
    return v[11:16] if len(v) >= 16 else ""


def parse(T):
    sessions = {_i(s["SWIMSESSIONID"]): str(s.get("STARTDATE") or "")[:10] for s in T.get("SWIMSESSION", [])}
    styles = {_i(s["SWIMSTYLEID"]): s for s in T.get("SWIMSTYLE", [])}
    events = {_i(e["SWIMEVENTID"]): e for e in T.get("SWIMEVENT", [])}
    clubs = {_i(c["CLUBID"]): (c.get("CODE") or c.get("SHORTNAME") or "").strip() for c in T.get("CLUB", [])}
    athletes = {_i(a["ATHLETEID"]): a for a in T.get("ATHLETE", [])}
    lanes, ev_gender = {}, {}
    heats = {_i(h["HEATID"]): h for h in T.get("HEAT", [])}
    for r in T.get("SWIMRESULT", []):
        hid, lane, a = _i(r.get("HEATID")), _i(r.get("LANE")), athletes.get(_i(r.get("ATHLETEID")))
        if hid not in heats or lane is None or not 0 <= lane < 20 or not a:
            continue
        et = _i(r.get("ENTRYTIME"))
        lanes.setdefault(hid, {})[lane] = {"lane": lane, "firstName": (a.get("FIRSTNAME") or "").strip(),
                                          "lastName": (a.get("LASTNAME") or "").strip(),
                                          "club": clubs.get(_i(a.get("CLUBID")), ""), "team": "",
                                          "entryTime": et * 10 if et and 0 < et < NT else None, "status": ""}
        g = _i(a.get("GENDER"))
        if g in (1, 2):
            ev_gender.setdefault(_i(heats[hid]["SWIMEVENTID"]), Counter())[g] += 1
    for r in T.get("RELAY", []):
        hid, lane = _i(r.get("HEATID")), _i(r.get("LANE"))
        if hid not in heats or lane is None or not 0 <= lane < 20:
            continue
        code = clubs.get(_i(r.get("CLUBID")), "")
        tn = _i(r.get("TEAMNUMBER"))
        et = _i(r.get("ENTRYTIME"))
        lanes.setdefault(hid, {})[lane] = {"lane": lane, "firstName": "", "lastName": "", "club": code,
                                          "team": (r.get("NAME") or "").strip() or f"{code} {tn or 1}",
                                          "entryTime": et * 10 if et and 0 < et < NT else None, "status": ""}
    out = []
    for hid, h in heats.items():
        ev = events.get(_i(h.get("SWIMEVENTID")))
        if not ev:
            continue
        st = styles.get(_i(ev.get("SWIMSTYLEID"))) or {}
        legs, dist = max(1, _i(st.get("RELAYCOUNT")) or 1), _i(st.get("DISTANCE")) or 0
        eg = _i(ev.get("GENDER"))
        if eg in (1, 2):
            gtxt = {1: "heren", 2: "dames"}[eg]
        else:
            g = ev_gender.get(_i(ev.get("SWIMEVENTID")))
            if not g:
                gtxt = "gemengd" if legs > 1 or eg == 3 else ""
            else:
                top, n = g.most_common(1)[0]
                gtxt = {1: "heren", 2: "dames"}[top] if n >= 0.8 * sum(g.values()) else "gemengd"
        disc = (f"{legs}x{dist}m" if legs > 1 else f"{dist}m") + " " + \
            (STROKES.get(_i(st.get("STROKE"))) or (st.get("NAME") or "")) + " " + gtxt
        hn = _i(h.get("HEATNUMBER")) or 0
        ls = sorted((lanes.get(hid) or {}).values(), key=lambda x: x["lane"])
        sid = _i(ev.get("SWIMSESSIONID"))
        out.append({"session": str(sid), "date": sessions.get(sid, ""),
                    "event": {"number": str(_i(ev.get("EVENTNUMBER")) or ""), "discipline": " ".join(disc.split())},
                    "heat": {"number": str(hn), "name": f"Serie {hn}", "distance": legs * dist,
                             "startTime": _hhmm(h.get("DAYTIME")) or _hhmm(ev.get("DAYTIME"))},
                    "relay": legs > 1, "finished": False, "lanes": ls, "lanesKnown": bool(ls), "source": "meetmanager"})
    out.sort(key=lambda x: (x["date"], x["heat"]["startTime"] or "99:99", int(x["event"]["number"] or 0),
                            -int(x["heat"]["number"] or 0)))
    stats = {"events": len({x["event"]["number"] for x in out}), "heats": len(out),
             "entries": sum(len(x["lanes"]) for x in out), "athletes": len(athletes),
             "clubs": len([c for c in clubs.values() if c]), "styles": len(styles)}
    return out, stats


def parse_file(path):
    T = read_tables(path)
    if not T.get("HEAT") or not T.get("SWIMEVENT"):
        raise ValueError("geen Meet Manager-wedstrijddatabank (tabellen HEAT/SWIMEVENT ontbreken of zijn leeg)")
    return parse(T)


if __name__ == "__main__":
    prog, stats = parse_file(sys.argv[1])
    print(stats)
    for x in prog[:12]:
        print(x["date"], x["heat"]["startTime"], "W" + x["event"]["number"], x["event"]["discipline"], x["heat"]["name"],
              "|", ", ".join(f"{l['lane']}:{l['firstName'] or l['team']} {l['lastName']} ({l['club']}) {l['entryTime']}"
                             for l in x["lanes"][:3]))
