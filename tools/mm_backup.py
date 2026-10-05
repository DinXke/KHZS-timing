"""Leest het programma (wedstrijden, reeksen, baanindeling, inschrijftijden) uit een Splash Meet Manager-backup (.smb).

Een .smb is een zip met .gbin-tabellen (eigen binair formaat van Geologix, niet gedocumenteerd). Velden die leeg
mogen zijn dragen soms een extra merkbyte, waardoor records geen vaste lengte hebben. Deze lezer splitst daarom
niet in records maar zoekt elk gegeven via herkenbare patronen en verwijzingen:
  - reeks: tijd-van-de-dag (TDateTime op 1/1/1800), gevolgd door reeksnummer en wedstrijd-ID
  - zwemmer: ID, club-ID, voornaam, VOORNAAM (5 letters), geslacht, achternaam, ACHTERNAAM
  - club: club-ID gevolgd door een korte code in hoofdletters
  - inschrijving: inschrijftijd, 'F/T', reeks-ID, baan, 'F/T' (+ zwemmer-ID aan het begin van het record)
  - wedstrijd: via de ID's waar de reeksen naar verwijzen; slag/afstand via de zwemslag-ID
Enkel lezen. Geboortedata en andere persoonsgegevens buiten naam/club worden NIET overgenomen.

Test:  python tools/mm_backup.py pad\\naar\\backup.smb   (of een map met uitgepakte .gbin-bestanden)
"""
import io
import os
import re
import struct
import sys
import zipfile
from collections import Counter
from datetime import datetime, timedelta

STROKES = {1: "Vrije slag", 2: "Rugslag", 3: "Schoolslag", 4: "Vlinderslag", 5: "Wisselslag"}
D1800 = bytes([0xD5, 0xE1, 0xC0])          # hoogste bytes van een TDateTime op 1/1/1800 (enkel een uur)
PRINT = set(range(32, 127)) | set(range(128, 256))      # UTF-8 (É = c3 89) en cp1252


def _header_end(b):
    return 2 + struct.unpack_from("<H", b, 0)[0]


def _i32(b, p):
    return struct.unpack_from("<i", b, p)[0] if 0 <= p and p + 4 <= len(b) else None


def _i16(b, p):
    return struct.unpack_from("<h", b, p)[0] if 0 <= p and p + 2 <= len(b) else None


def _str_at(b, p, lo=1, hi=60):
    """Lengte-voorafgegane string op p (2 bytes lengte), of None."""
    if p + 2 > len(b):
        return None
    n = struct.unpack_from("<H", b, p)[0]
    if not lo <= n <= hi or p + 2 + n > len(b):
        return None
    raw = b[p + 2:p + 2 + n]
    if not all(c in PRINT for c in raw):
        return None
    try:
        txt = raw.decode("utf-8")
    except UnicodeDecodeError:
        txt = raw.decode("cp1252", "replace")
    return txt, p + 2 + n


def _fold(s):
    """Hoofdletters zonder accenten en zonder spaties/koppeltekens (zoals Meet Manager de zoekvelden opslaat)."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s.upper())
    return "".join(c for c in s if c.isalnum())


def _tdt(raw):
    v = struct.unpack("<d", raw)[0]
    days = int(v)
    return datetime(1899, 12, 30) + timedelta(days=days) + timedelta(seconds=round(abs(v - days) * 86400))


def _skip_flag(b, p):
    """Sla een eventuele merkbyte (00/01) over vóór een int; geeft kandidaat-posities."""
    return [p, p + 1]


def parse(data):
    zf = zipfile.ZipFile(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data)
    names = {n.split("/")[-1].split("-")[0].upper(): n for n in zf.namelist() if n.lower().endswith(".gbin")}
    missing = [t for t in ("SWIMSTYLE", "SWIMEVENT", "HEAT", "CLUB", "ATHLETE", "SWIMRESULT") if t not in names]
    if missing:
        raise ValueError("geen Meet Manager-backup (ontbrekende tabellen: " + ", ".join(missing) + ")")
    T = {t: zf.read(n) for t, n in names.items()}

    # ---- zwemslagen: vaste kop (ID, CODE, DISTANCE, NAME, RELAYCOUNT, STROKE); ID's 1001.. oplopend
    styles = {}
    b = T["SWIMSTYLE"]
    p = _header_end(b)
    while p + 16 <= len(b):
        sid = _i32(b, p)
        s1 = _str_at(b, p + 4, 0, 10)
        if sid and 1000 < sid < 100000 and s1:
            q = s1[1]
            dist = _i16(b, q)
            s2 = _str_at(b, q + 2, 0, 50)
            if dist and 10 <= dist <= 1500 and s2:
                relay, stroke = _i16(b, s2[1]), _i16(b, s2[1] + 2)
                if relay is not None and 0 <= relay <= 10 and stroke is not None and 0 <= stroke <= 20:
                    styles.setdefault(sid, {"distance": dist, "relay": max(1, relay), "stroke": stroke, "name": s2[0]})
                    p = s2[1] + 4
                    continue
        p += 1

    # ---- reeksen: elke tijd op 1/1/1800, gevolgd door FINALCODE(S) HEATNUMBER RACESTATUS REMARKS(memo) SORTCODE SWIMEVENTID
    heats = {}
    b = T["HEAT"]
    p = b.find(D1800, _header_end(b))
    while p >= 0:
        q = p - 5
        try:
            t = _tdt(b[q:q + 8])
            if t.year == 1800:
                end = q + 8
                hnum, evid = _i16(b, end + 2), _i32(b, end + 14)
                # het reeks-ID staat 13 of 14 bytes vóór de tijd
                # eerst 13 (gewone record), dan 14 (met extra merkbyte); 14 bij een gewone record geeft ID<<8 → te groot
                hid = next((_i32(b, q - k) for k in (13, 14) if _i32(b, q - k) and 1000 < _i32(b, q - k) < 10**7), None)
                if hid and hnum and 0 < hnum < 200 and evid and evid > 1000:
                    heats[hid] = {"number": hnum, "time": t.strftime("%H:%M"), "event": evid}
        except (OverflowError, ValueError, struct.error):
            pass
        p = b.find(D1800, p + 1)

    # ---- wedstrijden: enkel de ID's waar reeksen naar verwijzen
    events = {}
    b = T["SWIMEVENT"]
    hdr = _header_end(b)
    for evid in {h["event"] for h in heats.values()}:
        for m in re.finditer(re.escape(struct.pack("<i", evid)), b[hdr:]):
            p = hdr + m.start()
            mlen = _i32(b, p + 4)
            if mlen is None or not 0 <= mlen < 4000:
                continue
            q = p + 8 + mlen
            if b[q + 5:q + 8] != D1800 or b[q + 13:q + 16] != D1800:
                continue
            day = _tdt(b[q:q + 8]).strftime("%H:%M")
            nums = [_i16(b, q + 16 + k) for k in (4, 5, 6)]           # EVENTNUMBER (merkbyte-varianten)
            evnum = next((n for n in (nums[2], nums[1], nums[0]) if n and 0 < n < 1000), None)
            sm = None
            for mm in re.finditer(rb"(?s)(.{4})(.{4})\x01\x00[FT]", b[q:q + 2000]):
                st = struct.unpack("<i", mm.group(2))[0]
                if st in styles:
                    sm = (struct.unpack("<i", mm.group(1))[0], st)
                    break
            if evnum and sm:
                events[evid] = {"number": evnum, "time": day, "session": sm[0], "style": sm[1]}
                break

    # ---- clubs: per gezocht club-ID de eerste korte code in hoofdletters erna
    CB = T["CLUB"]
    chdr = _header_end(CB)

    def club_code(cid):
        b = CB
        for m in re.finditer(re.escape(struct.pack("<i", cid)), b[chdr:]):
            p = chdr + m.start()
            for k in range(4, 18):
                s = _str_at(b, p + k, 2, 12)
                if s and re.fullmatch(r"[A-Z0-9][A-Z0-9 .&'-]{1,11}", s[0]):
                    return s[0]
        return ""

    # ---- zwemmers: ID CLUBID VOORNAAM VOORNAAM-HOOFDLETTERS GESLACHT ACHTERNAAM ACHTERNAAM-HOOFDLETTERS
    athletes = {}
    b = T["ATHLETE"]
    p = _header_end(b)
    clubs = {}
    while p + 12 <= len(b):
        f = _str_at(b, p + 8, 1, 40)
        if f:
            fu = _str_at(b, f[1], 1, 10)
            if fu and _fold(f[0]).startswith(_fold(fu[0])):
                g = _i16(b, fu[1])
                ln = _str_at(b, fu[1] + 2, 1, 60)
                if ln and g in (0, 1, 2):
                    lu = _str_at(b, ln[1], 1, 20)
                    if lu and _fold(ln[0]).startswith(_fold(lu[0])):
                        aid, cid = _i32(b, p), _i32(b, p + 4)
                        if cid not in clubs:
                            clubs[cid] = club_code(cid)
                        athletes[aid] = {"firstName": f[0], "lastName": ln[0], "club": clubs[cid], "gender": g}
                        p = lu[1]
                        continue
        p += 1

    # ---- inschrijvingen
    # FINALFIX(S1) FINISHJUDGE(2) [merk] HEATID(4) INFOCODE(leeg) [merk] LANE(2) LATEENTRY(S1); ENTRYTIME = 4 bytes ervoor
    entry_rx = re.compile(rb"(?s)(.{4})\x01\x00[FT].{2}\x01?(.{4})\x00\x00\x00?(.{2})\x01\x00[FT]")

    ff_rx = re.compile(rb"\x01\x00[FT]")

    def find_entries(b, hdr):
        """FINALFIX 'F/T' · FINISHJUDGE(2) · [merk 00/01] · HEATID · INFOCODE(leeg) · [merk 00] · LANE · LATEENTRY 'F/T'."""
        for m in ff_rx.finditer(b, hdr):
            base = m.end() + 2
            for mk in (0, 1):
                hp = base + mk
                hid = _i32(b, hp)
                if hid not in heats or b[hp + 4:hp + 6] != b"\x00\x00":
                    continue
                for mk2 in (0, 1):
                    lp = hp + 6 + mk2
                    if b[lp + 2:lp + 4] == b"\x01\x00" and b[lp + 4:lp + 5] in (b"F", b"T"):
                        yield m.start(), hid, _i16(b, lp), _i32(b, m.start() - 4)
                        break
                else:
                    continue
                break

    def entries(b, owner_ok, back=320):
        out = []
        hdr = _header_end(b)
        for start, hid, lane, et in find_entries(b, hdr):
            # begin van het record: dichtstbijzijnde positie ervoor met een geldige eigenaar
            owner = None
            for q in range(start - 8, max(hdr, start - back), -1):
                o = owner_ok(b, q)
                if o is not None:
                    owner = o
                    break
            if owner is not None and lane is not None and 0 <= lane < 20:     # baden met baan 0 (0–9)
                out.append((hid, lane, et if et and 0 < et < 36000000 else None, owner))
        return out

    lanes = {}
    for hid, lane, et, aid in entries(T["SWIMRESULT"], lambda b, q: _i32(b, q + 4) if _i32(b, q + 4) in athletes
                                       and _i32(b, q) and _i32(b, q) > 1000 else None):
        a = athletes[aid]
        lanes.setdefault(hid, {})[lane] = {"lane": lane, "firstName": a["firstName"], "lastName": a["lastName"],
                                          "club": a["club"], "team": "", "entryTime": et * 10 if et else None,
                                          "status": "", "_g": a["gender"]}
    if "RELAY" in T:
        def relay_owner(b, q):
            """Kop van een aflossingsrecord: ID · AGEMAX · AGEMIN · AGETOTAL · ATHLETES · CLUBID · GENDER · NAME · RELAYCODE · TEAMNUMBER."""
            rid, cid = _i32(b, q), _i32(b, q + 14)
            amax, amin, atot = _i16(b, q + 4), _i16(b, q + 6), _i16(b, q + 8)
            if not (rid and rid > 1000 and cid in clubs and amax is not None and 0 <= amin <= amax <= 120
                    and atot is not None and 0 <= atot <= 600):
                return None
            nm = _str_at(b, q + 20, 0, 100)
            if not nm:
                return None
            team = _i16(b, nm[1] + 4)
            return cid, nm[0], team if team and 0 < team < 30 else None
        # club-ID's: ook clubs die enkel met een aflossing deelnemen
        for cid in set(clubs):
            if not clubs[cid]:
                clubs[cid] = club_code(cid)
        for hid, lane, et, own in entries(T["RELAY"], relay_owner, back=200):
            cid, nm, teamnr = own
            code = clubs.get(cid) or ""
            if not teamnr:
                teamnr = sum(1 for l in lanes.get(hid, {}).values() if l["club"] == code) + 1
            lanes.setdefault(hid, {})[lane] = {"lane": lane, "firstName": "", "lastName": "", "club": code,
                                              "team": nm or f"{code} {teamnr}", "entryTime": et * 10 if et else None,
                                              "status": "", "_g": None}

    # ---- geslacht per wedstrijd uit de ingeschreven zwemmers
    ev_gender = {}
    for hid, ls in lanes.items():
        ev = heats[hid]["event"]
        for l in ls.values():
            if l["_g"] in (1, 2):
                ev_gender.setdefault(ev, Counter())[l["_g"]] += 1

    sess_date = {}
    if "SWIMSESSION" in T:
        SB = T["SWIMSESSION"]
        for sid in {e["session"] for e in events.values()}:
            for m in re.finditer(re.escape(struct.pack("<i", sid)), SB[_header_end(SB):]):
                p = _header_end(SB) + m.start()
                for q in range(p + 4, min(len(SB) - 8, p + 400)):
                    if SB[q + 7] == 0x40 and 0xe0 <= SB[q + 6] <= 0xe7:
                        try:
                            dt = _tdt(SB[q:q + 8])
                        except (OverflowError, ValueError):
                            continue
                        if 2000 < dt.year < 2100:
                            sess_date[sid] = dt.date().isoformat()
                            break
                if sid in sess_date:
                    break

    out = []
    for hid, h in heats.items():
        ev = events.get(h["event"])
        if not ev:
            continue
        st = styles.get(ev["style"], {})
        legs, dist = st.get("relay", 1), st.get("distance", 0)
        g = ev_gender.get(h["event"])
        if not g:
            gtxt = "gemengd" if legs > 1 else ""
        else:
            top, n = g.most_common(1)[0]
            gtxt = {1: "heren", 2: "dames"}[top] if n >= 0.8 * sum(g.values()) else "gemengd"
        disc = (f"{legs}x{dist}m" if legs > 1 else f"{dist}m") + " " + (STROKES.get(st.get("stroke")) or st.get("name") or "") + " " + gtxt
        ls = [dict((k, v) for k, v in l.items() if k != "_g") for l in sorted((lanes.get(hid) or {}).values(), key=lambda x: x["lane"])]
        out.append({"session": str(ev["session"]), "date": sess_date.get(ev["session"], ""),
                    "event": {"number": str(ev["number"]), "discipline": " ".join(disc.split())},
                    "heat": {"number": str(h["number"]), "name": f"Serie {h['number']}", "distance": legs * dist,
                             "startTime": h["time"]},
                    "relay": legs > 1, "finished": False, "lanes": ls, "lanesKnown": bool(ls), "source": "meetmanager"})
    out.sort(key=lambda x: (x["date"], x["heat"]["startTime"] or "99:99", int(x["event"]["number"] or 0), -int(x["heat"]["number"] or 0)))
    stats = {"events": len({x["event"]["number"] for x in out}), "heats": len(out), "entries": sum(len(x["lanes"]) for x in out),
             "athletes": len(athletes), "clubs": len([c for c in clubs.values() if c]), "styles": len(styles)}
    return out, stats


def load(path_or_dir):
    if os.path.isdir(path_or_dir):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f in os.listdir(path_or_dir):
                if f.lower().endswith(".gbin"):
                    z.write(os.path.join(path_or_dir, f), f)
        return parse(buf.getvalue())
    return parse(open(path_or_dir, "rb").read())


if __name__ == "__main__":
    prog, stats = load(sys.argv[1])
    print(stats)
    for x in prog[:16]:
        print(x["heat"]["startTime"], "W" + x["event"]["number"], x["event"]["discipline"], x["heat"]["name"],
              "|", ", ".join(f"{l['lane']}:{l['firstName'] or l['team']} {l['lastName']} ({l['club']}) {l['entryTime']}"
                             for l in x["lanes"][:3]))
