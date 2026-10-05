"""Zoekt per .gbin-tabel welke velden een null-merkbyte hebben (00 = leeg, 01 = waarde volgt).

Een schema is goed als de hele tabel achter elkaar gelezen kan worden met stijgende ID's en leesbare tekst.
Gebruik: python tools/gbin_schema.py MAP TABEL [TABEL ...]
"""
import os
import struct
import sys

SIZES = {"I32": 4, "I16": 2, "I8": 1, "D32": 8, "F0": 8}


def header(b):
    n = struct.unpack_from("<H", b, 0)[0]
    fields = []
    for part in b[2:2 + n].decode("latin-1").split("\t"):
        if part:
            nm, ty, sz = part.split(";")
            fields.append((nm, ty + sz if ty in "ID" else ty + ("0" if ty in "FM" else ""), int(sz)))
    return fields, 2 + n


def kind(ty, sz):
    if ty == "I":
        return "I%d" % sz
    if ty == "D":
        return "D32"
    if ty == "F":
        return "F0"
    if ty == "M":
        return "M"
    return "S"


def read_field(b, p, k, nullable):
    if nullable:
        if p >= len(b):
            return None, None
        f = b[p]
        if f == 0:
            return None, p + 1
        if f != 1:
            return "ERR", None
        p += 1
    if k in SIZES:
        n = SIZES[k]
        if p + n > len(b):
            return "ERR", None
        raw = b[p:p + n]
        if k == "I32":
            return struct.unpack("<i", raw)[0], p + n
        if k == "I16":
            return struct.unpack("<h", raw)[0], p + n
        return raw, p + n
    if k == "S":
        if p + 2 > len(b):
            return "ERR", None
        n = struct.unpack_from("<H", b, p)[0]
        if n > 2000 or p + 2 + n > len(b):
            return "ERR", None
        t = b[p + 2:p + 2 + n]
        if any(c < 9 or (13 < c < 32) for c in t):
            return "ERR", None
        return t, p + 2 + n
    if k == "M":
        if p + 4 > len(b):
            return "ERR", None
        n = struct.unpack_from("<I", b, p)[0]
        if n > 100000 or p + 4 + n > len(b):
            return "ERR", None
        return b[p + 4:p + 4 + n], p + 4 + n
    return "ERR", None


def score(b, pos, kinds, flags, limit=None):
    """Aantal records dat achter elkaar gelezen kan worden; True als het bestand precies op is."""
    p, n, last = pos, 0, None
    while p < len(b):
        start = p
        for i, k in enumerate(kinds):
            v, p2 = read_field(b, p, k, flags[i])
            if v == "ERR" or p2 is None:
                return n, False
            if i == 0:
                if not isinstance(v, int) or (last is not None and v <= last):
                    return n, False
                last = v
            p = p2
        n += 1
        if limit and n >= limit:
            return n, True
    return n, p == len(b)


def solve(b, pos, fields, beam=64):
    kinds = [kind(nm_ty[1][0], nm_ty[2]) for nm_ty in fields]
    cand = [i for i, k in enumerate(kinds) if i > 0]          # ID nooit nullable
    # straal-zoeken: schema's opbouwen veld per veld, gescoord op hoeveel records ze lezen
    states = [tuple([False] * len(kinds))]
    for i in cand:
        nxt = []
        for st in states:
            for f in (False, True):
                s2 = list(st)
                s2[i] = f
                nxt.append(tuple(s2))
        scored = []
        for st in nxt:
            sc, done = score(b, pos, kinds, st, limit=400)
            scored.append((sc, done, st))
        scored.sort(key=lambda x: (-x[0], sum(x[2])))
        states = [s for _, _, s in scored[:beam]]
    best = None
    for st in states:
        sc, done = score(b, pos, kinds, st)
        if best is None or (done, sc) > (best[1], best[0]):
            best = (sc, done, st)
    return kinds, best


if __name__ == "__main__":
    d = sys.argv[1]
    for t in sys.argv[2:]:
        b = open(os.path.join(d, t + "-0001.gbin"), "rb").read()
        fields, pos = header(b)
        kinds, (sc, done, flags) = solve(b, pos, fields)
        print(f"== {t}: {sc} records, bestand volledig gelezen: {done}")
        print("   nullable:", [fields[i][0] for i, f in enumerate(flags) if f])
