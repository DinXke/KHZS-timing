"""Lezer voor Splash Meet Manager-backups (.smb = zip met .gbin-tabellen).

Formaat (afgeleid): 2 bytes LE = lengte van de veldenlijst, dan 'NAAM;TYPE;GROOTTE\\t...',
daarna de records. Veldtypes: I (int 16/32 LE), S (string, 2-byte lengte + tekst),
D (datum, 32 bit), F (float), M (memo). Enkel lezen.
"""
import struct
import sys
import zipfile


def parse_header(b):
    n = struct.unpack_from("<H", b, 0)[0]
    txt = b[2:2 + n].decode("latin-1")
    fields = []
    for part in txt.split("\t"):
        if not part:
            continue
        name, typ, size = part.split(";")
        fields.append((name, typ, int(size)))
    return fields, 2 + n


def read_records(b, fields, pos, count, enc="cp1252", verbose=False):
    recs = []
    while pos < len(b) and (count is None or len(recs) < count):
        rec = {}
        start = pos
        try:
            for name, typ, size in fields:
                if typ == "I":
                    if size == 32:
                        rec[name] = struct.unpack_from("<i", b, pos)[0]; pos += 4
                    elif size == 16:
                        rec[name] = struct.unpack_from("<h", b, pos)[0]; pos += 2
                    elif size == 8:
                        rec[name] = b[pos]; pos += 1
                    else:
                        rec[name] = struct.unpack_from("<q", b, pos)[0]; pos += 8
                elif typ == "S" or typ == "M":
                    ln = struct.unpack_from("<H", b, pos)[0]; pos += 2
                    rec[name] = b[pos:pos + ln].decode(enc, "replace"); pos += ln
                elif typ == "D":
                    rec[name] = struct.unpack_from("<i", b, pos)[0]; pos += 4
                elif typ == "F":
                    rec[name] = struct.unpack_from("<d", b, pos)[0]; pos += 8
                elif typ == "B":
                    rec[name] = b[pos]; pos += 1
                else:
                    raise ValueError(f"onbekend type {typ}")
        except struct.error:
            break
        recs.append(rec)
        if verbose:
            print(f"record @{start} -> {pos}")
    return recs, pos


def load_table(zf, name, count=None, verbose=False):
    b = zf.read(f"{name}-0001.gbin")
    fields, pos = parse_header(b)
    recs, end = read_records(b, fields, pos, count, verbose=verbose)
    return fields, recs, end, len(b)


if __name__ == "__main__":
    zf = zipfile.ZipFile(sys.argv[1])
    for t in sys.argv[2:]:
        fields, recs, end, size = load_table(zf, t, verbose=False)
        print(f"== {t}: {len(recs)} records, gelezen tot {end}/{size} bytes")
        print("   velden:", ", ".join(f"{n}:{ty}{s}" for n, ty, s in fields))
        for r in recs[:3]:
            print("  ", {k: v for k, v in r.items() if v not in ("", 0, None)})
