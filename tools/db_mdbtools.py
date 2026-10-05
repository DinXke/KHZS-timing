"""SwimTime-database (kopie, .mdb) lezen op Linux met mdbtools – zelfde uitvoer als tools/db_export.ps1.

Wordt gebruikt op het kastje (Raspberry Pi); op Windows leest livetiming.py via de Access-driver (db_export.ps1).
Leest enkel een LOKALE KOPIE, nooit het origineel. mdbtools opent het bestand bovendien alleen lezend.

  python3 tools/db_mdbtools.py KOPIE.mdb [SESSIENUMMER]     → JSON zoals db_export.ps1
"""
import csv
import io
import json
import subprocess
import sys

DATEFMT = "%Y-%m-%d %H:%M:%S"


def table(path, name):
    r = subprocess.run(["mdb-export", "-D", DATEFMT, "-T", DATEFMT, "-b", "strip", path, name],
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        raise OSError(f"mdb-export {name}: " + r.stderr.decode("utf-8", "replace").strip()[:200])
    txt = r.stdout.decode("utf-8", "replace")
    return list(csv.DictReader(io.StringIO(txt)))


def num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except ValueError:
        return v


def boolv(v):
    return str(v).strip().lower() in ("1", "true", "-1", "yes", "ja")


def export(path, session_number=0):
    T = {n: table(path, n) for n in ("Sessions", "Event", "Heat", "Lanes", "Times", "RelayTeam", "RelaySet", "Competitors")}
    ids = lambda rows: {num(r["ID"]): r for r in rows}
    sessions = sorted(T["Sessions"], key=lambda r: -num(r["ID"]))
    sid = None
    for s in sessions:
        if session_number == 0 or num(s["SessionNumber"]) == session_number:
            sid = num(s["ID"])
            break
    if sid is None:
        return {"error": f"sessie {session_number} niet gevonden"}
    ev, heat, lanes = ids(T["Event"]), ids(T["Heat"]), ids(T["Lanes"])
    team, comp, sess = ids(T["RelayTeam"]), ids(T["Competitors"]), ids(T["Sessions"])

    def chain(lane_id):
        L = lanes.get(lane_id)
        H = heat.get(num(L["HeatID"])) if L else None
        E = ev.get(num(H["EventID"])) if H else None
        return L, H, E

    rows = []
    for t in T["Times"]:
        if (t.get("Status") or "") != "V":
            continue
        L, H, E = chain(num(t["LaneID"]))
        if not E or num(E["SessionID"]) != sid:
            continue
        rows.append([num(E["EventNr"]), num(H["HeatNumber"]), num(L["LaneNr"]), num(t["RelayNr"]), num(t["DayTime"]),
                     t.get("Channel") or "", t.get("Side") or "", t.get("Invers") or ""])
    rows.sort(key=lambda r: r[4] if r[4] is not None else 0)

    relays = []
    for rs in T["RelaySet"]:
        rt = team.get(num(rs["RelayID"]))
        c = comp.get(num(rs["CompID"]))
        if not rt or not c:
            continue
        for L in T["Lanes"]:
            if num(L.get("RelayTeamID")) != num(rt["ID"]):
                continue
            H = heat.get(num(L["HeatID"]))
            E = ev.get(num(H["EventID"])) if H else None
            if not E or num(E["SessionID"]) != sid:
                continue
            relays.append([num(E["EventNr"]), num(H["HeatNumber"]), num(L["LaneNr"]), rt.get("TeamName") or "",
                           num(rt.get("TeamNumber")), num(rs["RelayPosition"]), c.get("FirstName") or "",
                           c.get("LastName") or "", c.get("Club") or ""])
    relays.sort(key=lambda r: tuple(x if x is not None else -1 for x in (r[0], r[1], r[2], r[5])))

    meet = num(sess[sid]["MeetID"])
    lanes_by_heat = {}
    for L in T["Lanes"]:
        lanes_by_heat.setdefault(num(L["HeatID"]), []).append(L)
    sched = []
    for H in T["Heat"]:
        E = ev.get(num(H["EventID"]))
        S = sess.get(num(E["SessionID"])) if E else None
        if not S or num(S["MeetID"]) != meet:
            continue
        sdate = (S.get("Date") or "")[:10]
        stime = S.get("DayTime") or ""
        htime = (H.get("DayTime") or "")[11:16]
        base = [num(S["SessionNumber"]), sdate, num(E["EventNr"]), E.get("SwimStyleName") or "", num(E.get("Distance")),
                num(E.get("RelayCount")), num(H["HeatNumber"]), H.get("Name") or "", htime, boolv(H.get("Finished"))]
        order = (sdate, stime, num(E.get("Order")) or 0, num(H.get("Order")) or 0, num(H["HeatNumber"]) or 0)
        hl = lanes_by_heat.get(num(H["ID"])) or [None]
        for L in hl:
            if L is None:
                sched.append((order, -1, base + [None, None, "", "", "", "", ""]))
                continue
            c = comp.get(num(L.get("CompetitorID")))
            rt = team.get(num(L.get("RelayTeamID")))
            ln = num(L.get("LaneNr"))
            sched.append((order, ln if ln is not None else -1,
                          base + [ln, num(L.get("EntryTime")), L.get("Status") or "", (c or {}).get("FirstName") or "",
                                  (c or {}).get("LastName") or "", (c or {}).get("Club") or "", (rt or {}).get("TeamName") or ""]))
    sched.sort(key=lambda x: (x[0], x[1]))
    return {"session": session_number, "sessionId": sid, "rows": rows, "relays": relays, "schedule": [x[2] for x in sched]}


if __name__ == "__main__":
    out = export(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    sys.stdout.write(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
