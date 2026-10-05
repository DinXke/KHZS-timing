"""Leest het wedstrijdprogramma uit een Lenex-bestand (.lxf = zip, of .lef/.xml), zoals Splash Meet Manager het exporteert.

Resultaat in dezelfde vorm als de oproepkamer gebruikt (DB_SCHEDULE): per reeks wedstrijd, reeks, geplande tijd en
baanindeling met naam, club en inschrijftijd. Geboortedata en andere persoonsgegevens worden NIET overgenomen.
"""
import io
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

STROKES = {"FREE": "Vrije slag", "BACK": "Rugslag", "BREAST": "Schoolslag", "FLY": "Vlinderslag", "MEDLEY": "Wisselslag",
           "IMRELAY": "Wisselslag"}
GENDERS = {"M": "heren", "F": "dames", "X": "gemengd", "A": "gemengd"}


def _xml_bytes(data, filename=""):
    if data[:2] == b"PK":                        # .lxf = zip met één .lef
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = [i for i in zf.infolist() if i.filename.lower().endswith((".lef", ".xml"))]
        if not infos:
            raise ValueError("geen .lef-bestand in het Lenex-archief")
        if infos[0].file_size > 60 * 1024 * 1024:
            raise ValueError("Lenex-bestand is te groot")
        data = zf.read(infos[0])
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():      # geen DTD/entiteiten (XXE / billion laughs)
        raise ValueError("Lenex-bestand met DTD of entiteiten wordt geweigerd")
    return data


def _time(v):
    """Lenex-tijd 'HH:MM:SS.hh' -> 1/10000 s; 'NT' of leeg -> None."""
    m = re.match(r"^(\d+):(\d+):(\d+)\.(\d+)$", (v or "").strip())
    if not m:
        return None
    h, mi, s, frac = m.groups()
    frac = (frac + "00")[:2]
    return ((int(h) * 60 + int(mi)) * 60 + int(s)) * 10000 + int(frac) * 100


def _attr(el, name, default=""):
    for k, v in el.attrib.items():
        if k.lower() == name:
            return v
    return default


def _children(el, tag):
    return [c for c in el.iter() if c.tag.split("}")[-1].upper() == tag]


def parse(data, filename=""):
    root = ET.fromstring(_xml_bytes(data, filename))
    if root.tag.split("}")[-1].upper() != "LENEX":
        raise ValueError("geen Lenex-bestand")
    meets = _children(root, "MEET")
    if not meets:
        raise ValueError("geen wedstrijd (MEET) in het Lenex-bestand")
    meet = meets[0]

    events, heats = {}, {}
    for ses in _children(meet, "SESSION"):
        date = _attr(ses, "date")
        for ev in _children(ses, "EVENT"):
            sw = (_children(ev, "SWIMSTYLE") or [None])[0]
            dist = int(_attr(sw, "distance", "0") or 0) if sw is not None else 0
            legs = int(_attr(sw, "relaycount", "1") or 1) if sw is not None else 1
            stroke = _attr(sw, "stroke") if sw is not None else ""
            name = _attr(sw, "name") if sw is not None else ""
            disc = (f"{legs}x{dist}m" if legs > 1 else f"{dist}m") + " " + (STROKES.get(stroke.upper()) or name or stroke) + \
                " " + GENDERS.get(_attr(ev, "gender").upper(), "")
            eid = _attr(ev, "eventid")
            events[eid] = {"number": _attr(ev, "number"), "discipline": " ".join(disc.split()), "legs": legs, "dist": dist,
                           "session": _attr(ses, "number"), "date": date, "time": _attr(ev, "daytime")}
            for h in _children(ev, "HEAT"):
                heats[_attr(h, "heatid")] = {"event": eid, "number": _attr(h, "number"), "time": _attr(h, "daytime"),
                                             "lanes": {}}

    for club in _children(meet, "CLUB"):
        code = _attr(club, "code") or _attr(club, "shortname") or _attr(club, "name")
        for ath in _children(club, "ATHLETE"):
            for en in _children(ath, "ENTRY"):
                h = heats.get(_attr(en, "heatid"))
                lane = _attr(en, "lane")
                if h and lane.isdigit():
                    h["lanes"][int(lane)] = {"lane": int(lane), "firstName": _attr(ath, "firstname"),
                                             "lastName": ((_attr(ath, "nameprefix") + " ") if _attr(ath, "nameprefix") else "") + _attr(ath, "lastname"),
                                             "club": code, "team": "", "entryTime": _time(_attr(en, "entrytime")),
                                             "status": _attr(en, "status")}
        for rel in _children(club, "RELAY"):
            team = _attr(rel, "name") or (f"{code} {_attr(rel, 'number')}".strip())
            for en in _children(rel, "ENTRY"):
                h = heats.get(_attr(en, "heatid"))
                lane = _attr(en, "lane")
                if h and lane.isdigit():
                    h["lanes"][int(lane)] = {"lane": int(lane), "firstName": "", "lastName": "", "club": code, "team": team,
                                             "entryTime": _time(_attr(en, "entrytime")), "status": _attr(en, "status")}

    out = []
    for hid, h in heats.items():
        ev = events[h["event"]]
        ls = sorted(h["lanes"].values(), key=lambda x: x["lane"])
        out.append({"session": ev["session"], "date": ev["date"],
                    "event": {"number": ev["number"], "discipline": ev["discipline"]},
                    "heat": {"number": h["number"], "name": f"Serie {h['number']}", "distance": ev["legs"] * ev["dist"],
                             "startTime": (h["time"] or ev["time"] or "")[:5]},
                    "relay": ev["legs"] > 1, "finished": False, "lanes": ls, "lanesKnown": bool(ls), "source": "lenex"})

    def num(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    out.sort(key=lambda x: (x["date"], x["heat"]["startTime"] or "99:99", num(x["event"]["number"]), -num(x["heat"]["number"])))
    stats = {"meet": _attr(meet, "name"), "events": len(events), "heats": len(out),
             "entries": sum(len(x["lanes"]) for x in out)}
    return out, stats


if __name__ == "__main__":
    prog, st = parse(open(sys.argv[1], "rb").read(), sys.argv[1])
    print(st)
    for x in prog[:10]:
        print(x["date"], x["heat"]["startTime"], x["event"]["number"], x["event"]["discipline"], x["heat"]["name"], len(x["lanes"]), "banen")
