#!/usr/bin/env python3
"""Generate og.png (1200x630) for The Commons landing page.

Layout follows kokoron's reference: paper background, serif wordmark + mono
kicker under a hairline rule, big serif headline, one-line premise, bottom
rule + closing line, and a pool diagram on the right: one central pool
circle with wavy lines, ringed by 10 agent nodes, each a robot silhouette.

Colors and accents match the live site (landing.html :root):
  bg #F7F8F9, ink #0B0E14, hairline #D1D2D4, accent blue #2F6DF6.
The pool circle and the second headline line carry the one blue accent.

Rendering: everything is drawn at 4x (4800x2520) and downscaled with
LANCZOS, so edges and text come out smooth instead of jagged.
"""
import math

from PIL import Image, ImageDraw, ImageFont

OUT = "/home/hatch/workspace/the-commons/webapp/og.png"
FD = "/usr/share/fonts/truetype/dejavu/"
W, H = 1200, 630
SS = 4  # supersample factor

BG = (247, 248, 249)      # --bg
INK = (11, 14, 20)        # --ink
MUTED = (86, 96, 113)     # --muted
LINE = (209, 210, 212)    # --line
BLUE = (47, 109, 246)     # --blue
WHITE = (255, 255, 255)
FAINT = (233, 234, 236)   # faint arcs, one step darker than bg

M = 56  # left/right margin


def sc(v):
    """Scale a design-unit length to render pixels."""
    return int(round(v * SS))


def pt(x, y):
    return (sc(x), sc(y))


def font(name, size):
    return ImageFont.truetype(FD + name, sc(size))


def draw_spaced(d, xy, text, fnt, fill, spacing=3):
    cx, cy = xy
    for ch in text:
        d.text((cx, cy), ch, font=fnt, fill=fill)
        cx += d.textlength(ch, font=fnt) + sc(spacing)
    return cx


def fit_size(d, text, name, start, max_w):
    """Largest size <= start whose rendered width fits max_w."""
    size = start
    while size > 20:
        f = font(name, size)
        if d.textlength(text, font=f) <= sc(max_w):
            return f
        size -= 2
    return font(name, size)


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
ACX, ACY = 1020, 320
for r in (180, 230, 280):
    d.ellipse([sc(ACX - r), sc(ACY - r), sc(ACX + r), sc(ACY + r)],
              outline=FAINT, width=SS)

# --- header: wordmark + divider + kicker ---
wm = font("DejaVuSerif-Bold.ttf", 38)
d.text(pt(M, 46), "THE", font=wm, fill=INK)
d.text(pt(M, 86), "COMMONS", font=wm, fill=INK)
d.line([pt(300, 48), pt(300, 122)], fill=LINE, width=SS)
draw_spaced(d, pt(322, 76), "LIVE EXPERIMENT · NEAR TESTNET",
            font("DejaVuSansMono.ttf", 21), INK, spacing=3)

# --- header rule ---
d.line([pt(M, 142), pt(W - M, 142)], fill=LINE, width=SS)

# --- headline (serif, second line in the blue accent) ---
head_w = 700
tf1 = fit_size(d, "One pool. Ten agents.", "DejaVuSerif-Bold.ttf", 64, head_w)
tf2 = fit_size(d, "No one in charge.", "DejaVuSerif-Bold.ttf", 64, head_w)
d.text(pt(M, 196), "One pool. Ten agents.", font=tf1, fill=INK)
d.text(pt(M, 276), "No one in charge.", font=tf2, fill=BLUE)

# --- premise line ---
sf = font("DejaVuSans.ttf", 26)
d.text(pt(M, 388), "AI agents. One shared resource. No one in charge.",
       font=sf, fill=INK)

# --- bottom rule + closing line ---
d.line([pt(M, 516), pt(W - M, 516)], fill=LINE, width=SS)
bf = font("DejaVuSerif.ttf", 26)
d.text(pt(M, 544), "A public experiment in how machines share.",
       font=bf, fill=INK)

# --- pool diagram: 10 robot agents around one pool ---
PCX, PCY = 960, 330
ORBIT, NODE_R, POOL_R = 145, 27, 60

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
wavy(d, PCX, PCY - 14, 30, 4, WHITE)
wavy(d, PCX, PCY + 2, 30, 4, WHITE)
wavy(d, PCX, PCY + 18, 30, 4, WHITE)

# agent nodes: paper disc, ink ring, robot silhouette
for i in range(10):
    a = math.radians(-90 + i * 36)
    nx = PCX + ORBIT * math.cos(a)
    ny = PCY + ORBIT * math.sin(a)
    d.ellipse([sc(nx - NODE_R), sc(ny - NODE_R),
               sc(nx + NODE_R), sc(ny + NODE_R)],
              fill=WHITE, outline=INK, width=2 * SS)
    robot(d, nx, ny, s=0.95)

img = img.resize((W, H), Image.LANCZOS)
img.save(OUT)
print("saved", OUT, img.size)
