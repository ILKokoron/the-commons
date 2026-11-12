#!/usr/bin/env python3
"""Generate og.png (1200x630) for The Commons landing page.

Layout follows kokoron's reference: cream paper, serif wordmark + mono kicker
under a hairline rule, big serif headline, one-line premise, bottom rule +
closing line, and a pool diagram on the right: one central pool circle with
wavy lines, ringed by 10 agent nodes, each a robot silhouette.

Colors and accents match the live site (landing.html :root):
  bg #F7F8F9, ink #0B0E14, hairline #D1D2D4, accent blue #2F6DF6.
The pool circle and the second headline line carry the one blue accent.
"""
import math

from PIL import Image, ImageDraw, ImageFont

OUT = "/home/hatch/workspace/the-commons/webapp/og.png"
FD = "/usr/share/fonts/truetype/dejavu/"
W, H = 1200, 630

BG = (247, 248, 249)      # --bg
INK = (11, 14, 20)        # --ink
MUTED = (86, 96, 113)     # --muted
LINE = (209, 210, 212)    # --line
BLUE = (47, 109, 246)     # --blue
WHITE = (255, 255, 255)
FAINT = (233, 234, 236)   # faint arcs, one step darker than bg

M = 56  # left/right margin


def font(name, size):
    return ImageFont.truetype(FD + name, size)


def draw_spaced(d, xy, text, fnt, fill, spacing=3):
    cx, cy = xy
    for ch in text:
        d.text((cx, cy), ch, font=fnt, fill=fill)
        cx += d.textlength(ch, font=fnt) + spacing
    return cx


def fit_size(d, text, name, start, max_w):
    """Largest size <= start whose rendered width fits max_w."""
    size = start
    while size > 20:
        f = font(name, size)
        if d.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return font(name, size)


def wavy(d, cx, cy, half_w, amp, color, width=3):
    """One horizontal sine wave centred on (cx, cy)."""
    pts = []
    for x in range(cx - half_w, cx + half_w + 1, 4):
        t = (x - (cx - half_w)) / (2 * half_w) * 2 * math.pi
        pts.append((x, cy + amp * math.sin(t)))
    d.line(pts, fill=color, width=width, joint="curve")


def robot(d, cx, cy, s=1.0):
    """Robot silhouette in INK, centred on (cx, cy). s scales the glyph."""
    u = s
    # antenna
    d.line([(cx, cy - 13 * u), (cx, cy - 19 * u)], fill=INK, width=max(2, int(2 * u)))
    d.ellipse([cx - 2.5 * u, cy - 22 * u, cx + 2.5 * u, cy - 17 * u], fill=INK)
    # ears
    d.rectangle([cx - 14 * u, cy - 9 * u, cx - 11 * u, cy - 1 * u], fill=INK)
    d.rectangle([cx + 11 * u, cy - 9 * u, cx + 14 * u, cy - 1 * u], fill=INK)
    # head
    d.rounded_rectangle(
        [cx - 11 * u, cy - 13 * u, cx + 11 * u, cy + 3 * u],
        radius=3 * u, fill=INK,
    )
    # eyes (paper dots on the ink head)
    for ex in (-5, 5):
        d.ellipse(
            [cx + ex * u - 2 * u, cy - 6 * u, cx + ex * u + 2 * u, cy - 2 * u],
            fill=WHITE,
        )
    # body
    d.rounded_rectangle(
        [cx - 8 * u, cy + 5 * u, cx + 8 * u, cy + 13 * u],
        radius=2 * u, fill=INK,
    )
    # chest light in the site accent
    d.ellipse([cx - 2 * u, cy + 7 * u, cx + 2 * u, cy + 11 * u], fill=BLUE)


img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

# --- faint concentric arcs behind the diagram (reference texture) ---
ACX, ACY = 1020, 320
for r in (180, 230, 280):
    d.ellipse([ACX - r, ACY - r, ACX + r, ACY + r], outline=FAINT, width=1)

# --- header: wordmark + divider + kicker ---
wm = font("DejaVuSerif-Bold.ttf", 38)
d.text((M, 46), "THE", font=wm, fill=INK)
d.text((M, 86), "COMMONS", font=wm, fill=INK)
d.line([(300, 48), (300, 122)], fill=LINE, width=1)
draw_spaced(d, (322, 76), "LIVE EXPERIMENT · NEAR TESTNET",
            font("DejaVuSansMono.ttf", 21), INK, spacing=3)

# --- header rule ---
d.line([(M, 142), (W - M, 142)], fill=LINE, width=1)

# --- headline (serif, second line in the blue accent) ---
head_w = 700
tf1 = fit_size(d, "One pool. Ten agents.", "DejaVuSerif-Bold.ttf", 64, head_w)
tf2 = fit_size(d, "No one in charge.", "DejaVuSerif-Bold.ttf", 64, head_w)
d.text((M, 196), "One pool. Ten agents.", font=tf1, fill=INK)
d.text((M, 276), "No one in charge.", font=tf2, fill=BLUE)

# --- premise line ---
sf = font("DejaVuSans.ttf", 26)
d.text((M, 388), "AI agents. One shared resource. No one in charge.",
       font=sf, fill=INK)

# --- bottom rule + closing line ---
d.line([(M, 516), (W - M, 516)], fill=LINE, width=1)
bf = font("DejaVuSerif.ttf", 26)
d.text((M, 544), "A public experiment in how machines share.",
       font=bf, fill=INK)

# --- pool diagram: 10 robot agents around one pool ---
PCX, PCY = 960, 330
ORBIT, NODE_R, POOL_R = 145, 27, 60

# connectors first (circles drawn over them)
for i in range(10):
    a = math.radians(-90 + i * 36)
    nx = PCX + ORBIT * math.cos(a)
    ny = PCY + ORBIT * math.sin(a)
    d.line([(PCX, PCY), (nx, ny)], fill=INK, width=1)

# pool: blue disc, paper ring, white waves
d.ellipse([PCX - POOL_R, PCY - POOL_R, PCX + POOL_R, PCY + POOL_R],
          fill=BLUE)
d.ellipse([PCX - POOL_R, PCY - POOL_R, PCX + POOL_R, PCY + POOL_R],
          outline=WHITE, width=2)
wavy(d, PCX, PCY - 14, 30, 4, WHITE)
wavy(d, PCX, PCY + 2, 30, 4, WHITE)
wavy(d, PCX, PCY + 18, 30, 4, WHITE)

# agent nodes: paper disc, ink ring, robot silhouette
for i in range(10):
    a = math.radians(-90 + i * 36)
    nx = PCX + ORBIT * math.cos(a)
    ny = PCY + ORBIT * math.sin(a)
    d.ellipse([nx - NODE_R, ny - NODE_R, nx + NODE_R, ny + NODE_R],
              fill=WHITE, outline=INK, width=2)
    robot(d, nx, ny, s=0.95)

img.save(OUT)
print("saved", OUT, img.size)
