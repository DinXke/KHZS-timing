"""Vectoriseert het HZS-logo (appliance/plymouth/bron/hzs-logo.png, 200x120) per kleur met potrace.

  pip install potracer numpy
  python tools/vectorize_logo.py

Schrijft appliance/plymouth/bron/hzs-logo.svg (scherp op elke grootte, ook bruikbaar op de website) en
hzs-logo.json (vlakke polygonen in viewBox-eenheden, voor make_bootscreen.py zonder SVG-bibliotheek).
"""
import json
import os

import numpy as np
import potrace
from PIL import Image, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "appliance", "plymouth", "bron", "hzs-logo.png")
K = 10                                     # opschaling vóór het overtrekken
COLORS = {"licht": "#009fe0", "donker": "#074da2"}


def masks(im):
    a = np.asarray(im.convert("RGBA")).astype(float)
    alpha, g = a[..., 3] / 255.0, a[..., 1]
    return {"licht": alpha * (g > 118), "donker": alpha * (g <= 118)}


def trace(cov, size):
    w, h = size
    m = Image.fromarray((cov * 255).astype(np.uint8), "L").resize((w * K, h * K), Image.BICUBIC)
    m = m.filter(ImageFilter.GaussianBlur(K * 0.5))
    bm = potrace.Bitmap(np.asarray(m) < 128)        # potracer: False = voorgrond
    return bm.trace(turdsize=K * K * 2, alphamax=1.0, opticurve=True, opttolerance=0.2)


def bez(p0, p1, p2, p3, n=16):
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


def main():
    im = Image.open(SRC)
    w, h = im.size
    svg_paths, polys = [], {}
    for name, cov in masks(im).items():
        d, plist = [], []
        for curve in trace(cov, (w, h)):
            sp = curve.start_point
            cur = (sp.x / K, sp.y / K)
            d.append(f"M{cur[0]:.2f} {cur[1]:.2f}")
            poly = [cur]
            for seg in curve.segments:
                end = (seg.end_point.x / K, seg.end_point.y / K)
                if seg.is_corner:
                    c = (seg.c.x / K, seg.c.y / K)
                    d.append(f"L{c[0]:.2f} {c[1]:.2f}L{end[0]:.2f} {end[1]:.2f}")
                    poly += [c, end]
                else:
                    c1, c2 = (seg.c1.x / K, seg.c1.y / K), (seg.c2.x / K, seg.c2.y / K)
                    d.append(f"C{c1[0]:.2f} {c1[1]:.2f} {c2[0]:.2f} {c2[1]:.2f} {end[0]:.2f} {end[1]:.2f}")
                    poly += bez(cur, c1, c2, end)
                cur = end
            d.append("Z")
            plist.append([[round(x, 3), round(y, 3)] for x, y in poly])
        svg_paths.append(f'<path fill="{COLORS[name]}" fill-rule="evenodd" d="{"".join(d)}"/>')
        polys[name] = plist
    out = os.path.join(ROOT, "appliance", "plymouth", "bron")
    with open(os.path.join(out, "hzs-logo.svg"), "w", encoding="utf-8", newline="\n") as f:
        f.write(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w * 4}" height="{h * 4}">\n'
                + "\n".join(svg_paths) + "\n</svg>\n")
    with open(os.path.join(out, "hzs-logo.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"size": [w, h], "colors": COLORS, "polygons": polys}, f, separators=(",", ":"))
    print("klaar:", {k: len(v) for k, v in polys.items()}, "contouren")


if __name__ == "__main__":
    main()
