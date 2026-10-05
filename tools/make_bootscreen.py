"""Maakt de afbeeldingen van het opstartscherm (Plymouth-thema appliance/plymouth/khzs) en een voorbeeld.

  python tools/make_bootscreen.py            afbeeldingen + appliance/plymouth/preview.png

Stijl: donkere achtergrond van het beheer, bovenaan het HZS-logo met "Timing", onderaan de golf uit de
splash screens (lichtblauw boven, donkerblauw onder) met daarin wat het kastje aan het doen is.
Referentiegrootte 1920x1080; khzs.script schaalt mee. Maten hier en in khzs.script moeten overeenkomen.
"""
import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "appliance", "plymouth", "khzs")

BG, BG2, TEXT, MUTED = "#07131f", "#0a1928", "#e8f1f8", "#9bb2c4"
LIGHT, DARK = "#009fe0", "#074da2"           # kleuren van het HZS-logo
FOAM = "#38bdf8"

# golven (referentie 1080 hoog): bovenkant van elke kam-afbeelding, amplitude, periode
BACK_Y, MID_Y, FRONT_Y = 600, 622, 660
AMP, PERIOD = 16, 300
CREST_W = 2560 + PERIOD                      # breed genoeg tot 21:9 + één periode om te verschuiven
CREST_H = 110


def font(names, size):
    for n in names:
        for d in (r"C:\Windows\Fonts", "/usr/share/fonts/truetype/dejavu"):
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    return ImageFont.load_default()


BOLD = ["segoeuib.ttf", "DejaVuSans-Bold.ttf"]
SEMI = ["seguisb.ttf", "DejaVuSans-Bold.ttf"]
REG = ["segoeui.ttf", "DejaVuSans.ttf"]


def rgba(h, a=255):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (a,)


def logo():
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from hzs_wordmark import wordmark
    return wordmark(170)


def crest(color, alpha=255, phase=0.0, amp=AMP):
    """Golfkam over de volle breedte; onderaan volledig gevuld (sluit aan op de effen vulling eronder)."""
    S = 2
    W, H = CREST_W * S, CREST_H * S
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pts = [(0, H)]
    for x in range(0, W + 1, 2):
        y = (amp + 6) * S + amp * S * math.sin(2 * math.pi * (x / S) / PERIOD + phase)
        pts.append((x, y))
    pts.append((W, H))
    ImageDraw.Draw(im).polygon(pts, fill=rgba(color, alpha))
    # schuimrand
    line = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(line).line(pts[1:-1], fill=rgba(FOAM if color == LIGHT else LIGHT, 130 if alpha == 255 else 70), width=3 * S)
    im.alpha_composite(line)
    return im.resize((CREST_W, CREST_H), Image.LANCZOS)


def solid(color, alpha=255):
    return Image.new("RGBA", (16, 16), rgba(color, alpha))


def bubble():
    S, R = 4, 18
    im = Image.new("RGBA", (R * S, R * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((S * 2, S * 2, (R - 2) * S, (R - 2) * S), outline=rgba("#bfe9ff", 190), width=int(1.6 * S))
    d.ellipse((S * 5, S * 5, S * 7.5, S * 7.5), fill=rgba("#ffffff", 170))
    return im.resize((R, R), Image.LANCZOS)


def bar(fill):
    W, H, S = 640, 8, 4
    im = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if fill:
        c0, c1 = rgba(FOAM), rgba("#ffffff")
        for x in range(W * S):
            t = x / (W * S)
            d.line((x, 0, x, H * S), fill=tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3)) + (255,))
        mask = Image.new("L", im.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, W * S - 1, H * S - 1), radius=H * S // 2, fill=255)
        im.putalpha(mask)
    else:
        d.rounded_rectangle((0, 0, W * S - 1, H * S - 1), radius=H * S // 2, fill=rgba("#03264f", 200))
    return im.resize((W, H), Image.LANCZOS)


def preview(imgs, shift=(90, 40, 170)):
    """Benadering van één frame zoals khzs.script het tekent (zelfde maten)."""
    W, H = 1920, 1080
    im = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(im)
    top, bot = rgba(BG), rgba(BG2)
    for y in range(H):
        t = y / H
        d.line((0, y, W, y), fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))
    lg = imgs["logo"]
    im.alpha_composite(lg, (W // 2 - lg.width // 2, int(H * 0.30 - lg.height / 2)))
    for name, fill, y, sh in (("crest_back", "fill_back", BACK_Y, shift[0]), ("crest_mid", None, MID_Y, shift[1]),
                              ("crest_front", "fill_front", FRONT_Y, shift[2])):
        im.alpha_composite(imgs[name].crop((sh, 0, sh + W, CREST_H)), (0, y))
        if fill:
            im.alpha_composite(imgs[fill].resize((W, H - y - CREST_H + 1)), (0, y + CREST_H - 1))
    for bx, by in ((300, 980), (340, 900), (1500, 1010), (1620, 940), (980, 1040)):
        im.alpha_composite(imgs["bubble"], (bx, by))
    f1, f2, f3, f4 = font(SEMI, 36), font(REG, 22), font(REG, 24), font(REG, 20)
    y = 778
    for txt, f, col, dy in (("Live timing gestart", f1, "#ffffff", 0), ("khzs-timing.service", f2, "#bcd7f0", 52)):
        d.text((W / 2 - f.getlength(txt) / 2, y + dy), txt, font=f, fill=rgba(col))
    bb, bf = imgs["bar_bg"], imgs["bar_fg"]
    by = y + 100
    im.alpha_composite(bb, (W // 2 - bb.width // 2, by))
    im.alpha_composite(bf.resize((int(bf.width * 0.62), bf.height)), (W // 2 - bb.width // 2, by))
    info = "server  ·  Ethernet 192.168.1.20  ·  http://khzs-timing.local  ·  versie 1.0.0"
    d.text((W / 2 - f3.getlength(info) / 2, by + 32), info, font=f3, fill=rgba("#ffffff"))
    foot = "Niet uittrekken tijdens het opstarten"
    d.text((W / 2 - f4.getlength(foot) / 2, H - 50), foot, font=f4, fill=rgba("#bcd7f0", 170))
    return im


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        if f.endswith(".png"):
            os.remove(os.path.join(OUT, f))
    imgs = {"logo": logo(),
            "crest_back": crest(LIGHT), "crest_mid": crest(FOAM, 90, phase=1.7, amp=12), "crest_front": crest(DARK, phase=3.1),
            "fill_back": solid(LIGHT), "fill_front": solid(DARK), "bubble": bubble(),
            "bar_bg": bar(False), "bar_fg": bar(True)}
    for k, im in imgs.items():
        im.save(os.path.join(OUT, k + ".png"), optimize=True)
    preview(imgs).convert("RGB").save(os.path.join(ROOT, "appliance", "plymouth", "preview.png"))
    print("klaar:", OUT)


if __name__ == "__main__":
    main()
