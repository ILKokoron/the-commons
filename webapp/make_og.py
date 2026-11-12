#!/usr/bin/env python3
"""Generate og.png (1200x630) for The Commons landing page.

A faithful copy of kokoron's reference layout (1733x907, scaled to 1200x630),
with the colors swapped to the live site palette (landing.html :root):
  reference cream (246,241,229) -> site bg #F7F8F9
  reference ink                 -> site ink #0B0E14
  reference olive pool          -> site accent blue #2F6DF6
  reference hairlines           -> site line #D1D2D4
Everything else (layout, type, diagram) follows the reference exactly:
Didone serif (Noto Serif Display) for wordmark / headline / premise lines,
letterspaced grotesque for the kicker, one pool ringed by 10 robot agents.

Rendering: drawn at 4x (4800x2520) and downscaled with LANCZOS, so edges
and text come out smooth instead of jagged.
"""
import math

from PIL import Image, ImageDraw, ImageFont

OUT = "/home/hatch/workspace/the-commons/webapp/og.png"
FD = "/usr/share/fonts/truetype/dejavu/"
NOTO = "/usr/share/fonts/truetype/noto/"
W, H = 1200, 630
SS = 4  # supersample factor

BG = (247, 248, 249)      # --bg
INK = (11, 14, 20)        # --ink
LINE = (209, 210, 212)    # --line
BLUE = (47, 109, 246)     # --blue
WHITE = (255, 255, 255)
FAINT = (233, 234, 236)   # faint arcs, one step darker than bg

M = 64  # left margin (reference wordmark starts here)


def sc(v):
    """Scale a design-unit length to render pixels."""
    return int(round(v * SS))


def pt(x, y):
    return (sc(x), sc(y))


def dfont(name, size):
    return ImageFont.truetype(FD + name, sc(size))


def nfont(name, size):
    return ImageFont.truetype(NOTO + name, sc(size))


def draw_spaced(d, xy, text, fnt, fill, spacing=2):
    cx, cy = xy
    for ch in text:
        d.text((cx, cy), ch, font=fnt, fill=fill)
        cx += d.textlength(ch, font=fnt) + sc(spacing)
    return cx


def fit_size(d, text, name, start, max_w, noto=True):
    """Largest size <= start whose rendered width fits max_w."""
    size = start
    while size > 20:
        f = nfont(name, size) if noto else dfont(name, size)
        if d.textlength(text, font=f) <= sc(max_w):
            return f
        size -= 2
    return (nfont(name, size) if noto else dfont(name, size))


def wavy(d, cx, cy, half_w, amp, color, width=3):
    """One horizontal sine wave centred on (cx, cy)."""
    pts = []
    for x in range(sc(cx - half_w), sc(cx + half_w) + 1, 4):
        t = (x - sc(cx - half_w)) / (2 * sc(half_w)) * 2 * math.pi
        pts.append((x, sc(cy) + sc(amp) * math.sin(t)))
    d.line(pts, fill=color, width=sc(width), joint="curve")


def robot(d, cx, cy, s=1.0):
    """Robot silhouette in INK, centred on (cx, cy). s scales the glyph."""
    u = s * SS
    # antenna
    d.line([pt(cx, cy - 13 * s), pt(cx, cy - 19 * s)],
           fill=INK, width=max(2, int(2 * u)))
    d.ellipse([sc(cx - 2.5 * s), sc(cy - 22 * s),
               sc(cx + 2.5 * s), sc(cy - 17 * s)], fill=INK)
    # ears
    d.rectangle([sc(cx - 14 * s), sc(cy - 9 * s),
                 sc(cx - 11 * s), sc(cy - 1 * s)], fill=INK)
    d.rectangle([sc(cx + 11 * s), sc(cy - 9 * s),
                 sc(cx + 14 * s), sc(cy - 1 * s)], fill=INK)
    # head
    d.rounded_rectangle(
        [sc(cx - 11 * s), sc(cy - 13 * s),
         sc(cx + 11 * s), sc(cy + 3 * s)],
        radius=sc(3 * s), fill=INK,
    )
    # eyes (paper dots on the ink head)
    for ex in (-5, 5):
        d.ellipse(
            [sc(cx + ex * s - 2 * s), sc(cy - 6 * s),
             sc(cx + ex * s + 2 * s), sc(cy - 2 * s)],
            fill=WHITE,
        )
    # body
    d.rounded_rectangle(
        [sc(cx - 8 * s), sc(cy + 5 * s),
         sc(cx + 8 * s), sc(cy + 13 * s)],
        radius=sc(2 * s), fill=INK,
    )
    # chest light in the site accent
    d.ellipse([sc(cx - 2 * s), sc(cy + 7 * s),
               sc(cx + 2 * s), sc(cy + 11 * s)], fill=BLUE)


img = Image.new("RGB", (W * SS, H * SS), BG)
d = ImageDraw.Draw(img)

# --- faint concentric arcs behind the diagram (reference texture) ---
ACX, ACY = 924, 318
for r in (190, 240, 290):
    d.ellipse([sc(ACX - r), sc(ACY - r), sc(ACX + r), sc(ACY + r)],
              outline=FAINT, width=SS)

# --- header: Didone wordmark + divider + letterspaced kicker ---
wm_the = nfont("NotoSerifDisplay-Bold.ttf", 26)
wm_com = nfont("NotoSerifDisplay-Bold.ttf", 48)
d.text(pt(M, 54), "THE", font=wm_the, fill=INK)
d.text(pt(M, 78), "COMMONS", font=wm_com, fill=INK)
div_x = M + d.textlength("COMMONS", font=wm_com) / SS + 20
d.line([pt(div_x, 54), pt(div_x, 112)], fill=LINE, width=SS)
draw_spaced(d, pt(div_x + 20, 74), "LIVE EXPERIMENT · NEAR TESTNET",
            dfont("DejaVuSans.ttf", 15), INK, spacing=2)

# --- header rule ---
d.line([pt(M, 130), pt(W - M, 130)], fill=LINE, width=SS)

# --- headline (Didone, all ink like the reference) ---
head_w = 690
tf1 = fit_size(d, "One pool. Ten agents.", "NotoSerifDisplay-Bold.ttf", 72, head_w)
tf2 = fit_size(d, "No one in charge.", "NotoSerifDisplay-Bold.ttf", 72, head_w)
d.text(pt(M, 222), "One pool. Ten agents.", font=tf1, fill=INK)
d.text(pt(M, 322), "No one in charge.", font=tf2, fill=INK)

# --- premise line (Didone regular) ---
sf = fit_size(d, "AI agents. One shared resource. No one in charge.",
              "NotoSerifDisplay-Regular.ttf", 32, head_w)
d.text(pt(M, 420), "AI agents. One shared resource. No one in charge.",
       font=sf, fill=INK)

# --- bottom rule + closing line ---
d.line([pt(M, 524), pt(W - M, 524)], fill=LINE, width=SS)
bf = nfont("NotoSerifDisplay-Regular.ttf", 28)
d.text(pt(M, 552), "A public experiment in how machines share.",
       font=bf, fill=INK)

# --- pool diagram: 10 robot agents around one pool ---
PCX, PCY = 924, 318
ORBIT, NODE_R, POOL_R = 160, 19, 55

# connectors first (circles drawn over them)
for i in range(10):
    a = math.radians(-90 + i * 36)
    nx = PCX + ORBIT * math.cos(a)
    ny = PCY + ORBIT * math.sin(a)
    d.line([pt(PCX, PCY), pt(nx, ny)], fill=INK, width=SS)

# pool: blue disc, paper ring, white waves
d.ellipse([sc(PCX - POOL_R), sc(PCY - POOL_R),
           sc(PCX + POOL_R), sc(PCY + POOL_R)], fill=BLUE)
d.ellipse([sc(PCX - POOL_R), sc(PCY - POOL_R),
           sc(PCX + POOL_R), sc(PCY + POOL_R)], outline=WHITE, width=2 * SS)
wavy(d, PCX, PCY - 13, 28, 4, WHITE)
wavy(d, PCX, PCY + 2, 28, 4, WHITE)
wavy(d, PCX, PCY + 17, 28, 4, WHITE)

# agent nodes: paper disc, ink ring, robot silhouette
for i in range(10):
    a = math.radians(-90 + i * 36)
    nx = PCX + ORBIT * math.cos(a)
    ny = PCY + ORBIT * math.sin(a)
    d.ellipse([sc(nx - NODE_R), sc(ny - NODE_R),
               sc(nx + NODE_R), sc(ny + NODE_R)],
              fill=WHITE, outline=INK, width=2 * SS)
    robot(d, nx, ny, s=0.8)

img = img.resize((W, H), Image.LANCZOS)
img.save(OUT)
print("saved", OUT, img.size)
