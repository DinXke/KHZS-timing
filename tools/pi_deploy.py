"""Nieuwe code naar een KHZS-kastje sturen via SSH (zonder opnieuw te flashen).

  python tools/pi_deploy.py khzs-server.local            pakket bouwen, kopiëren, installeren (zelftest + terugval)
  python tools/pi_deploy.py khzs-server.local --dev      bestanden rechtstreeks naar /home/khzs/dev + herstart (snel testen)
  python tools/pi_deploy.py khzs-server.local --user beheerder

Gebruikt de OpenSSH-client van Windows (ssh/scp). Met een SSH-sleutel (Gebruikers & SSH in het beheer, of
ssh_sleutel in khzs-instellingen.txt) hoef je geen wachtwoord te typen; sudo kan wel om je wachtwoord vragen.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sh(cmd, **kw):
    print("$ " + " ".join(cmd))
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        sys.exit(f"mislukt ({r.returncode})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", help="naam of IP van het kastje, bv. khzs-server.local")
    ap.add_argument("--user", default="khzs")
    ap.add_argument("--dev", action="store_true", help="enkel bestanden kopiëren naar de ontwikkelmap en herstarten")
    a = ap.parse_args()
    target = f"{a.user}@{a.host}"
    sh([sys.executable, os.path.join(ROOT, "tools", "build_release.py")])
    ver = open(os.path.join(ROOT, "livetiming.py"), encoding="utf-8").read().split('VERSION = "', 1)[1].split('"', 1)[0]
    pkg = os.path.join(ROOT, "dist", f"khzs-timing-{ver}.zip")
    if a.dev:
        tmp = tempfile.mkdtemp()
        with zipfile.ZipFile(pkg) as z:
            for n in z.namelist():
                if n.startswith("app/"):
                    z.extract(n, tmp)
        sh(["ssh", target, "mkdir -p /home/khzs/dev"])
        sh(["scp", "-r", "-q"] + [os.path.join(tmp, "app", x) for x in os.listdir(os.path.join(tmp, "app"))] + [f"{target}:/home/khzs/dev/"])
        sh(["ssh", "-t", target, "sudo khzs dev on /home/khzs/dev && khzs status"])
    else:
        sh(["scp", "-q", pkg, f"{target}:/tmp/"])
        sh(["ssh", "-t", target, f"sudo khzs update /tmp/{os.path.basename(pkg)} && khzs system pending && khzs status"])
    print("klaar")


if __name__ == "__main__":
    main()
