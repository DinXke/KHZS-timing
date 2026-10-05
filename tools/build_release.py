"""Maakt een updatepakket voor de wedstrijdserver (Raspberry Pi): dist/khzs-timing-<versie>.zip

Inhoud: release.json (versie + SHA-256 per bestand) en de code onder app/. Gegevens (instellingen, historiek,
databases, logs) zitten er NOOIT in. Te uploaden via /system → Updates, of als asset van een GitHub-release.

Gebruik:  python tools/build_release.py            (pakket in dist/)
          python tools/build_release.py --dir out  (ook uitgepakt in out/app, voor het image)
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ["livetiming.py", "simulator.py", "README.md", "tools/mm_backup.py", "tools/gbin.py", "tools/lenex.py",
         "tools/build_release.py", "tools/db_mdbtools.py", "cloud/cloud_agent.py"]
DIRS = [("static", (".html", ".css", ".js", ".svg", ".png", ".ico", ".woff2")), ("static/vendor", (".js", ".css", ".txt")), ("static/img", (".png", ".svg")),
        ("appliance/agent", (".py",)), ("appliance/systemd", (".service",)), ("appliance/bin", ("khzs",)),
        ("appliance/conf", (".conf", ".json", "motd")), ("appliance/system", (".sh", ".txt")),
        ("appliance/system/migrations", (".sh", ".txt")),
        ("appliance/plymouth", (".conf",)), ("appliance/plymouth/khzs", (".png", ".script", ".plymouth"))]


def version(root=None):
    m = re.search(r'^VERSION = "([^"]+)"', open(os.path.join(root or ROOT, "livetiming.py"), encoding="utf-8").read(), re.M)
    if not m:
        sys.exit("VERSION niet gevonden in livetiming.py")
    return m.group(1)


def collect(root=None):
    root = root or ROOT
    out = []
    for f in FILES:
        if os.path.exists(os.path.join(root, f)):
            out.append(f)
    for d, exts in DIRS:
        full = os.path.join(root, d)
        if not os.path.isdir(full):
            continue
        for name in sorted(os.listdir(full)):
            if os.path.isfile(os.path.join(full, name)) and name.lower().endswith(exts):
                out.append(f"{d}/{name}")
    return out


TEXT_EXT = (".py", ".html", ".md", ".sh", ".service", ".conf", ".txt", ".json", ".script", ".plymouth", ".css", ".js")


def write_zip(fileobj, root=None):
    """Releasepakket schrijven (app/... + release.json met SHA-256 per bestand). Geeft de versie terug."""
    root = root or ROOT
    ver = version(root)
    meta = {"version": ver, "built": time.strftime("%Y-%m-%d %H:%M:%S"), "files": {}}
    files = collect(root)
    with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            data = open(os.path.join(root, f), "rb").read()
            if f.endswith(TEXT_EXT) or f.endswith(("motd", "/khzs")):
                data = data.replace(b"\r\n", b"\n")
            meta["files"][f] = hashlib.sha256(data).hexdigest()
            z.writestr("app/" + f, data)
        z.writestr("app/VERSION", ver + "\n")
        meta["files"]["VERSION"] = hashlib.sha256((ver + "\n").encode()).hexdigest()
        z.writestr("release.json", json.dumps(meta, indent=1))
    return ver, len(files) + 1


def build_bytes(root=None):
    """Zelfde pakket in het geheugen (de lokale server werkt zo de cloudserver bij)."""
    import io
    bio = io.BytesIO()
    ver, _ = write_zip(bio, root)
    return bio.getvalue(), ver


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="ook uitpakken in deze map (map/app/...)")
    a = ap.parse_args()
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    ver = version()
    zpath = os.path.join(ROOT, "dist", f"khzs-timing-{ver}.zip")
    with open(zpath, "wb") as fo:
        ver, nfiles = write_zip(fo)
    print(f"{zpath}  ({nfiles} bestanden, versie {ver})")
    if a.dir:
        dst = os.path.join(a.dir, "app")
        shutil.rmtree(dst, ignore_errors=True)
        with zipfile.ZipFile(zpath) as z:
            for n in z.namelist():
                if n.startswith("app/"):
                    p = os.path.join(a.dir, n)
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "wb") as f:
                        f.write(z.read(n))
        print(f"uitgepakt in {dst}")


if __name__ == "__main__":
    main()
