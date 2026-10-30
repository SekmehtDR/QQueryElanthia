"""Draw Fennick, the storyteller persona: a Halfling Bard in a round medallion, in two frames (mouth closed / open).

Run:  python tools/make_fennick.py          (needs Pillow: pip install -e ".[dev]")
Writes src/epedia/frontends/web/static/fennick.png and fennick_talk.png. The web page swaps the two while he
"speaks". Everything is drawn at 4x and scaled down for clean edges.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parents[1] / "src" / "epedia" / "frontends" / "web" / "static"
S = 1024  # working size
FINAL = 256  # delivered size

# Palette, picked to sit well on the page's dark warm theme.
SKIN, SKIN_SHADE, BLUSH = "#f0c39a", "#dca77c", (232, 120, 100, 120)
HAIR, HAIR_LIGHT = "#7a4a24", "#9a6431"
GREEN, GREEN_DARK, GOLD = "#4f7a4a", "#3a5f38", "#d9a066"
SCARF, SCARF_DARK = "#c4572a", "#9a3f1d"
FEATHER, FEATHER_DARK = "#d24a3a", "#a63126"
WOOD, WOOD_DARK = "#b06a30", "#5b3416"
INK = "#3a2412"


def bezier(p0, p1, p2, n=24):
    pts = []
    for i in range(n + 1):
        t = i / n
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t**2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t**2 * p2[1]
        pts.append((x, y))
    return pts


def circle(d: ImageDraw.ImageDraw, c, r, fill, outline=None, width=0):
    d.ellipse((c[0] - r, c[1] - r, c[0] + r, c[1] + r), fill=fill, outline=outline, width=width)


def background(d: ImageDraw.ImageDraw) -> None:
    for i in range(40):  # soft radial glow: warm centre, dark rim
        t = i / 39
        r = 520 * (1 - t)
        shade = (int(40 + 62 * t), int(31 + 48 * t), int(20 + 32 * t))
        circle(d, (512, 480), r, shade)


def body(d: ImageDraw.ImageDraw) -> None:
    d.ellipse((150, 770, 874, 1250), fill=GREEN, outline=GREEN_DARK, width=10)  # tunic shoulders
    d.arc((300, 800, 724, 1100), 200, 340, fill=GREEN_DARK, width=10)
    d.ellipse((445, 640, 579, 760), fill=SKIN_SHADE)  # neck
    d.ellipse((350, 690, 674, 830), fill=SCARF, outline=SCARF_DARK, width=8)  # scarf
    d.polygon([(560, 780), (690, 800), (720, 950), (640, 920), (610, 860)], fill=SCARF, outline=SCARF_DARK)
    for x in (580, 625, 670):
        d.line((x, 800, x + 12, 900), fill=SCARF_DARK, width=7)  # scarf stripes


def lute(d: ImageDraw.ImageDraw) -> None:
    d.polygon([(330, 800), (372, 770), (250, 520), (204, 548)], fill=WOOD_DARK)  # neck
    d.polygon([(204, 548), (250, 520), (236, 488), (190, 512)], fill=GOLD, outline=WOOD_DARK)  # headstock
    d.ellipse((140, 740, 410, 975), fill=WOOD, outline=WOOD_DARK, width=10)  # body
    d.ellipse((140, 740, 410, 975), outline="#d58a45", width=4)
    circle(d, (278, 860), 40, "#2b1a0c", outline=WOOD_DARK, width=8)  # sound hole
    for k in range(3):  # strings
        d.line((300 + k * 8, 800 + k * 4, 222 + k * 8, 536 + k * 4), fill="#f4e7c8", width=3)


def head(d: ImageDraw.ImageDraw, open_mouth: bool) -> None:
    for sx in (-1, 1):  # ears, slightly pointed
        cx = 512 + sx * 215
        d.polygon([(cx - sx * 15, 480), (cx + sx * 85, 455), (cx + sx * 5, 610)], fill=SKIN, outline=SKIN_SHADE)
        d.polygon([(cx - sx * 4, 500), (cx + sx * 50, 488), (cx + sx * 4, 580)], fill=SKIN_SHADE)
    d.ellipse((312, 270, 712, 740), fill=SKIN)  # face
    # blush and freckles
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    circle(ld, (385, 580), 55, BLUSH)
    circle(ld, (639, 580), 55, BLUSH)
    d._image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(14)))
    for dx, dy in ((-40, -8), (-8, 22), (30, -22), (62, 14), (4, -34)):
        circle(d, (385 + dx, 585 + dy), 6, "#c98a63")
        circle(d, (639 - dx, 585 + dy), 6, "#c98a63")
    # eyes: big, friendly
    for cx in (432, 592):
        d.ellipse((cx - 44, 470, cx + 44, 560), fill="#fff8ee", outline=INK, width=6)
        circle(d, (cx + 4, 518), 28, "#3b2a1c")
        circle(d, (cx + 4, 518), 14, "#120a05")
        circle(d, (cx + 16, 505), 9, "white")
    d.line(bezier((372, 450), (432, 405), (486, 436)), fill=HAIR, width=15)  # brows, raised and curious
    d.line(bezier((538, 436), (592, 405), (652, 450)), fill=HAIR, width=15)
    # nose
    d.ellipse((486, 540, 540, 600), fill="#e6ac82")
    d.ellipse((498, 548, 518, 566), fill="#f6d0ae")
    # a small, well-kept moustache
    d.polygon(bezier((512, 612), (462, 598), (418, 628)) + bezier((418, 628), (470, 640), (512, 626)), fill=HAIR)
    d.polygon(bezier((512, 612), (562, 598), (606, 628)) + bezier((606, 628), (554, 640), (512, 626)), fill=HAIR)
    # mouth
    if open_mouth:
        d.pieslice((452, 616, 572, 706), 0, 180, fill="#5a1f1a", outline=INK, width=6)
        d.pieslice((478, 662, 546, 706), 0, 180, fill="#d4736b")
    else:
        d.arc((446, 596, 578, 690), 28, 152, fill=INK, width=9)
        circle(d, (452, 644), 5, INK)
        circle(d, (572, 644), 5, INK)


def hair_and_hat(d: ImageDraw.ImageDraw) -> None:
    for cx, cy, r, c in [(335, 350, 62, HAIR), (300, 440, 56, HAIR), (322, 530, 46, HAIR), (689, 350, 62, HAIR),
                         (724, 440, 56, HAIR), (702, 530, 46, HAIR), (400, 322, 58, HAIR_LIGHT),
                         (470, 312, 60, HAIR), (550, 312, 60, HAIR_LIGHT), (626, 322, 58, HAIR)]:
        circle(d, (cx, cy), r, c)
    d.polygon([(318, 560), (352, 540), (346, 640), (324, 612)], fill=HAIR)  # sideburns
    d.polygon([(706, 560), (672, 540), (678, 640), (700, 612)], fill=HAIR)
    # floppy cap: crown flops to one side, gold band, brim
    crown = bezier((330, 320), (350, 120), (560, 90)) + bezier((560, 90), (720, 80), (800, 190)) \
        + bezier((800, 190), (740, 250), (700, 330))
    d.polygon(crown, fill=GREEN, outline=GREEN_DARK)
    d.line(bezier((420, 190), (520, 150), (640, 160)), fill="#6a9a5f", width=10)  # highlight
    d.rounded_rectangle((318, 300, 706, 358), radius=26, fill=GOLD, outline="#a87a3d", width=6)  # band
    d.line((330, 332, 694, 332), fill="#f0c88a", width=5)
    # feather tucked into the band
    leaf = bezier((640, 318), (700, 140), (880, 110)) + bezier((880, 110), (850, 240), (700, 330))
    d.polygon(leaf, fill=FEATHER, outline=FEATHER_DARK)
    d.line(bezier((660, 322), (730, 200), (860, 124)), fill=FEATHER_DARK, width=6)
    for t in (0.3, 0.5, 0.7):  # barbs
        x, y = bezier((660, 322), (730, 200), (860, 124))[int(t * 24)]
        d.line((x, y, x + 36, y + 24), fill=FEATHER_DARK, width=4)


LARGE = 512  # the About page's portrait, big enough to see the feather and the lute strings


def render(open_mouth: bool, size: int = FINAL) -> Image.Image:
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    background(d)
    body(d)
    lute(d)
    head(d, open_mouth)
    hair_and_hat(d)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse((24, 24, S - 24, S - 24), fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(base, (0, 0), mask)
    ring = ImageDraw.Draw(out)
    ring.ellipse((24, 24, S - 24, S - 24), outline=GOLD, width=22)  # medallion rim
    ring.ellipse((46, 46, S - 46, S - 46), outline="#7a5a33", width=5)
    return out.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    render(False).save(OUT / "fennick.png", optimize=True)
    render(True).save(OUT / "fennick_talk.png", optimize=True)
    render(False, LARGE).save(OUT / "fennick_large.png", optimize=True)
    print(f"wrote {OUT / 'fennick.png'}, {OUT / 'fennick_talk.png'} and {OUT / 'fennick_large.png'}")


if __name__ == "__main__":
    main()
