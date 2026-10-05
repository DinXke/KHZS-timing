"""Woordmerk "HZS TIMING": het gevectoriseerde HZS-logo + TIMING in dezelfde stijl (helling, afgeronde hoeken,
golf doorheen de letters: lichtblauw boven, donkerblauw onder, witte snede ertussen).

  from hzs_wordmark import wordmark
  im = wordmark(height=150, white=False)      # PIL RGBA; white=True = volledig wit (voor op lichtblauw)

Het lettertype van het logo zelf is niet bekend (getekend logo); TIMING gebruikt Arial Black/DejaVu Sans Bold
schuin gezet en afgerond. Met het echte lettertype (bestand in appliance/plymouth/bron/) wordt het exact.
"""
import json
import math
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRON = os.path.join(ROOT, "appliance", "plymouth", "bron")
LIGHT, DARK = "#009fe0", "#074da2"
SLANT = 0.17                     # helling van de letters in het logo (dx per eenheid hoogte)
NARROW = 0.8                     # horizontale verhouding t.o.v. het lettertype
GAP = 0.03                       # dikte van de witte golfsnede (deel van de letterhoogte)


def _font(size):
    for p in (os.path.join(BRON, "logo-font.ttf"), os.path.join(BRON, "logo-font.otf"),
              r"C:\Windows\Fonts\ariblk.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def logo_mask(height, S=4):
    """Maskers (licht, donker) van het gevectoriseerde logo op de gegeven hoogte."""
    d = json.load(open(os.path.join(BRON, "hzs-logo.json"), encoding="utf-8"))
    w, h = d["size"]
    sc = height * S / h
    size = (round(w * sc), height * S)
    out = {}
    for name in ("licht", "donker"):
        m = Image.new("1", size, 0)
        for poly in d["polygons"][name]:
            p = Image.new("1", size, 0)
            ImageDraw.Draw(p).polygon([(x * sc, y * sc) for x, y in poly], fill=1)
            m = ImageChops.logical_xor(m, p)
        out[name] = m.convert("L")
    return out, size


def timing_mask(height, S=4, text="TIMING"):
    """TIMING met hoofdletterhoogte = letterhoogte van het logo, schuin en afgerond."""
    H = height * S
    cap = 0.94 * H                                        # het logo vult de hoogte bijna volledig
    f = _font(int(cap / 0.72))
    bb = f.getbbox("H")
    f = _font(int(f.size * cap / (bb[3] - bb[1])))
    bb = f.getbbox("H")
    top = bb[1]
    track = -0.015 * cap
    widths = [f.getlength(c) for c in text]
    W = int(sum(widths) + track * (len(text) - 1) + SLANT * H + 0.1 * H)
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    x = 0.02 * H
    y0 = (H - cap) / 2 - top
    for c, wc in zip(text, widths):
        d.text((x, y0), c, font=f, fill=255)
        x += wc + track
    # smaller maken: de letters van het logo zijn smaller dan Arial Black
    m = m.resize((int(m.width * NARROW), m.height), Image.LANCZOS)
    W = m.width
    # schuin zetten: bovenkant naar rechts (zoals het logo)
    m = m.transform(m.size, Image.AFFINE, (1, SLANT, -SLANT * H, 0, 1, 0), resample=Image.BICUBIC)
    # hoeken afronden zoals in het logo
    m = m.filter(ImageFilter.GaussianBlur(0.035 * H)).point(lambda v: 255 if v > 128 else 0)
    bbox = m.getbbox()
    return m.crop((bbox[0], 0, bbox[2], H))


def wave_y(x, H):
    """Golf door TIMING, sluit aan op het einde van de golf in de S van het logo (≈0,77 van de hoogte)."""
    t = x / H
    return H * (0.655 + 0.115 * math.cos(2 * math.pi * t / 2.4))


def split(mask, H):
    """Masker opdelen in boven (licht) en onder (donker) met een witte golfsnede ertussen."""
    W = mask.width
    top = Image.new("L", mask.size, 0)
    bot = Image.new("L", mask.size, 0)
    pts = [(x, wave_y(x, H)) for x in range(0, W + 4, 4)]
    g = GAP * H / 2
    ImageDraw.Draw(top).polygon([(0, 0), (W, 0)] + [(x, y - g) for x, y in reversed(pts)], fill=255)
    ImageDraw.Draw(bot).polygon([(0, H), (W, H)] + [(x, y + g) for x, y in reversed(pts)], fill=255)
    return ImageChops.multiply(mask, top), ImageChops.multiply(mask, bot)


def wordmark(height=150, white=False, gap=0.16, S=4):
    lm, lsize = logo_mask(height, S)
    tm = timing_mask(height, S)
    tl, td = split(tm, height * S)
    G = int(gap * height * S)
    W = lsize[0] + G + tm.width
    im = Image.new("RGBA", (W, height * S), (0, 0, 0, 0))
    parts = ((lm["licht"], 0, LIGHT), (lm["donker"], 0, DARK), (tl, lsize[0] + G, LIGHT), (td, lsize[0] + G, DARK))
    for m, x, col in parts:
        layer = Image.new("RGBA", m.size, "#ffffff" if white else col)
        layer.putalpha(m)
        im.alpha_composite(layer, (x, 0))
    return im.resize((round(W / S), height), Image.LANCZOS)


if __name__ == "__main__":
    a = wordmark(300)
    b = wordmark(300, white=True)
    out = Image.new("RGBA", (max(a.width, b.width) + 80, 2 * 300 + 120), "#07131f")
    out.alpha_composite(a, (40, 30))
    blue = Image.new("RGBA", (out.width, 360), LIGHT)
    out.alpha_composite(blue, (0, 360))
    out.alpha_composite(b, (40, 390))
    p = os.path.join(BRON, "woordmerk-voorbeeld.png")
    out.convert("RGB").save(p)
    print(p)
