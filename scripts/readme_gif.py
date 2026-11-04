"""The animated header of README.md: requests travel through Jarvis on a dark circuit board.

The voice enters at the microphone, becomes text, reaches the reactor (Jarvis deciding), goes to one of three
places (a skill, the local model, the internet) and comes back as Jarvis's voice. Colors: the app's states
(listening green, thinking amber, speaking blue) and the icon's reactor.

    .venv\\Scripts\\python scripts\\readme_gif.py      -> media/jarvis-flow.gif (needs ffmpeg)
"""
from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "media" / "jarvis-flow.gif"

W, H = 900, 300          # final size
S = 2                    # drawn at 2x, then scaled down: smooth lines
FPS = 20
SEC = 7.2                # one loop
FRAMES = int(FPS * SEC)

BG = (12, 15, 20)
PANEL = (21, 26, 34)
LINE = (37, 44, 56)
TRACE = (30, 40, 54)
TEXT = (230, 234, 241)
MUTED = (139, 150, 168)
LISTEN = (76, 195, 138)
THINK = (224, 166, 74)
SPEAK = (109, 155, 240)
REACTOR = (42, 168, 224)
GLOW = (143, 220, 255)
DEEP = (11, 58, 92)

FONT = "C:/Windows/Fonts/segoeui.ttf"
FONT_SB = "C:/Windows/Fonts/seguisb.ttf"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_SB if bold else FONT, size * S)


def P(x: float, y: float) -> tuple[float, float]:
    return x * S, y * S


# ---------------------------------------------------------------- layout (1x coordinates)
MIC = (64, 150)
STT = (190, 150)          # box centre
ORB = (388, 150)
ORB_R = 76
BRANCH_X = 590
BRANCHES = [(BRANCH_X, 62, "навыки", "окна, музыка, таймеры"),
            (BRANCH_X, 150, "локальная ИИ", "qwen на вашем ПК"),
            (BRANCH_X, 238, "интернет", "свежие факты")]
TTS = (752, 150)
SPK = (848, 150)
BOX_W, BOX_H = 132, 46


def route(branch: tuple) -> list[tuple[float, float]]:
    """mic -> recognition -> reactor -> branch -> voice -> speaker, with right-angle turns like a maze."""
    bx, by = branch[0], branch[1]
    return [MIC, (STT[0], 150), (ORB[0], 150), (ORB[0] + ORB_R + 30, 150), (ORB[0] + ORB_R + 30, by),
            (bx, by), (bx + BOX_W / 2 + 24, by), (bx + BOX_W / 2 + 24, 150), (TTS[0], 150), SPK]


class Path2:
    def __init__(self, pts: list[tuple[float, float]]) -> None:
        self.pts = pts
        self.lens = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
        self.total = sum(self.lens)

    def at(self, s: float) -> tuple[float, float]:
        d = max(0.0, min(1.0, s)) * self.total
        for (a, b), ln in zip(zip(self.pts, self.pts[1:]), self.lens):
            if d <= ln:
                k = d / ln if ln else 0
                return a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k
            d -= ln
        return self.pts[-1]

    def frac_x(self, x: float) -> float:
        """Share of the path where it first reaches x (going right)."""
        d = 0.0
        for (a, b), ln in zip(zip(self.pts, self.pts[1:]), self.lens):
            if a[0] <= x <= b[0] and b[0] != a[0]:
                return (d + (x - a[0])) / self.total
            d += ln
        return 1.0


PATHS = [Path2(route(b)) for b in BRANCHES]

# Decorative traces: the "maze" around the real routes (dim, a few faint sparks run along them).
DECOR = [
    [(20, 40), (130, 40), (130, 90), (260, 90), (260, 30), (330, 30)],
    [(20, 262), (110, 262), (110, 214), (270, 214), (270, 276), (340, 276)],
    [(440, 18), (440, 40), (520, 40), (520, 18)],
    [(440, 284), (440, 262), (500, 262), (500, 284)],
    [(690, 22), (720, 22), (720, 96), (880, 96)],
    [(690, 280), (730, 280), (730, 206), (880, 206)],
    [(284, 116), (284, 56), (312, 56)],
    [(284, 184), (284, 274), (318, 274)],
]
DECOR_PATHS = [Path2(d) for d in DECOR]


# ---------------------------------------------------------------- drawing helpers
def mix(a: tuple, b: tuple, k: float) -> tuple:
    k = max(0.0, min(1.0, k))
    return tuple(round(x + (y - x) * k) for x, y in zip(a, b))


def polyline(d: ImageDraw.ImageDraw, pts, fill, width: float) -> None:
    pts2 = [P(*p) for p in pts]
    d.line(pts2, fill=fill, width=round(width * S), joint="curve")
    r = width * S / 2
    for x, y in (pts2[0], pts2[-1]):
        d.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def dot(d: ImageDraw.ImageDraw, p, r: float, fill) -> None:
    x, y = P(*p)
    d.ellipse((x - r * S, y - r * S, x + r * S, y + r * S), fill=fill)


def text_c(d: ImageDraw.ImageDraw, p, s: str, f, fill) -> None:
    x, y = P(*p)
    d.text((x, y), s, font=f, fill=fill, anchor="mm")


def box(d: ImageDraw.ImageDraw, c, title: str, sub: str, heat: float, color: tuple) -> None:
    x, y = c
    x0, y0, x1, y1 = P(x - BOX_W / 2, y - BOX_H / 2) + P(x + BOX_W / 2, y + BOX_H / 2)
    d.rounded_rectangle((x0, y0, x1, y1), radius=10 * S, fill=mix(PANEL, color, heat * 0.12),
                        outline=mix(LINE, color, heat), width=round(1.5 * S))
    text_c(d, (x, y - 8), title, font(14, True), mix(TEXT, (255, 255, 255), heat * 0.5))
    text_c(d, (x, y + 11), sub, font(10.5), mix(MUTED, color, heat * 0.6))


def packet_color(path: Path2, s: float) -> tuple:
    f_orb = path.frac_x(ORB[0])
    f_out = path.frac_x(TTS[0] - BOX_W / 2)
    if s < f_orb:
        return mix(LISTEN, THINK, (s - f_orb + 0.03) / 0.03)
    if s < f_out:
        return mix(THINK, SPEAK, (s - f_out + 0.03) / 0.03)
    return SPEAK


def near(p, q, r: float) -> float:
    """1 at q, falling to 0 at distance r."""
    return max(0.0, 1 - math.dist(p, q) / r)


# ---------------------------------------------------------------- one frame
def frame(i: int) -> Image.Image:
    t = i / FRAMES                      # 0..1 over the loop
    img = Image.new("RGB", (W * S, H * S), BG)
    glow = Image.new("RGB", (W * S, H * S), (0, 0, 0))
    d, g = ImageDraw.Draw(img), ImageDraw.Draw(glow)

    # grid of faint dots
    for gx in range(10, W, 20):
        for gy in range(10, H, 20):
            dot(d, (gx, gy), 0.8, (22, 27, 36))

    # decorative maze with vias and faint sparks
    for k, path in enumerate(DECOR_PATHS):
        polyline(d, path.pts, TRACE, 2)
        for p in (path.pts[0], path.pts[-1]):
            dot(d, p, 3.2, TRACE)
            dot(d, p, 1.4, BG)
        s = (t * (1 + k % 2) + k * 0.37) % 1
        x, y = path.at(s)
        dot(g, (x, y), 4, mix((0, 0, 0), REACTOR, 0.55))
        dot(d, (x, y), 1.6, mix(TRACE, GLOW, 0.7))

    # the real routes
    for path in PATHS:
        polyline(d, path.pts, (34, 46, 64), 3)

    packets = []
    for k, path in enumerate(PATHS):
        s = (t + k / len(PATHS)) % 1
        packets.append((path, s, path.at(s), packet_color(path, s)))

    def heat_at(c, r: float = 70) -> tuple[float, tuple]:
        best, col = 0.0, SPEAK
        for _, _, p, color in packets:
            h = near(p, c, r)
            if h > best:
                best, col = h, color
        return best, col

    # lit trail behind each packet: the stretch of route it just crossed
    for path, s, _, color in packets:
        steps = 28
        for j in range(steps):
            s0 = s - j * 0.0045
            if s0 < 0:
                break
            a, b = path.at(s0), path.at(s0 - 0.0045)
            fade = 1 - j / steps
            polyline(d, [b, a], mix((34, 46, 64), color, fade * 0.9), 3)
            if j < 10:
                polyline(g, [b, a], mix((0, 0, 0), color, fade * 0.6), 6)

    # packets: a bright head with a halo (under the boxes: a box lights up as one passes through)
    for _, _, p, color in packets:
        dot(g, p, 12, color)
        dot(d, p, 5, mix(color, (255, 255, 255), 0.35))
        dot(d, p, 2.4, (255, 255, 255))

    # microphone with live sound bars (louder while a packet leaves it)
    h_mic, _ = heat_at(MIC, 90)
    for k in range(5):
        amp = 0.35 + 0.65 * h_mic
        hgt = (6 + 16 * abs(math.sin(2 * math.pi * (t * 6 + k * 0.23)))) * amp
        x = MIC[0] - 44 + k * 6
        d.rounded_rectangle(P(x - 1.6, MIC[1] - hgt) + P(x + 1.6, MIC[1] + hgt), radius=2 * S,
                            fill=mix(LINE, LISTEN, 0.4 + 0.6 * amp))
    dot(g, MIC, 26, mix((0, 0, 0), LISTEN, 0.25 + 0.5 * h_mic))
    dot(d, MIC, 20, mix(PANEL, LISTEN, 0.15))
    d.ellipse(P(MIC[0] - 20, MIC[1] - 20) + P(MIC[0] + 20, MIC[1] + 20), outline=mix(LINE, LISTEN, 0.5 + 0.5 * h_mic),
              width=round(1.5 * S))
    d.rounded_rectangle(P(MIC[0] - 5, MIC[1] - 11) + P(MIC[0] + 5, MIC[1] + 3), radius=5 * S, fill=LISTEN)
    d.arc(P(MIC[0] - 9, MIC[1] - 8) + P(MIC[0] + 9, MIC[1] + 8), 20, 160, fill=LISTEN, width=round(2 * S))
    d.line(P(MIC[0], MIC[1] + 8) + P(MIC[0], MIC[1] + 13), fill=LISTEN, width=round(2 * S))
    text_c(d, (MIC[0], MIC[1] + 36), "«Джарвис, …»", font(11), MUTED)

    # recognition box
    h, c = heat_at(STT)
    box(d, STT, "распознавание", "речь → текст, на ПК", h, LISTEN)

    # branches
    for b in BRANCHES:
        h, c = heat_at((b[0], b[1]))
        box(d, (b[0], b[1]), b[2], b[3], h, THINK)

    # voice box and speaker
    h, c = heat_at(TTS)
    box(d, TTS, "голос", "Джарвис отвечает", h, SPEAK)
    h_spk, _ = heat_at(SPK, 90)
    dot(g, SPK, 24, mix((0, 0, 0), SPEAK, 0.2 + 0.5 * h_spk))
    x, y = SPK
    d.polygon([P(x - 12, y - 5), P(x - 6, y - 5), P(x + 1, y - 11), P(x + 1, y + 11), P(x - 6, y + 5),
               P(x - 12, y + 5)], fill=SPEAK)
    for k in range(3):
        ph = (t * 4 + k / 3) % 1
        r = 8 + ph * 22
        col = mix(BG, SPEAK, (1 - ph) * (0.4 + 0.6 * h_spk))
        d.arc(P(x + 2 - r, y - r) + P(x + 2 + r, y + r), -45, 45, fill=col, width=round(2 * S))

    # the reactor, drawn like the app icon (assistant/ui/icon.svg, radius 62 there): Jarvis deciding.
    # Turns amber while a request is inside; the segmented ring and the outer arcs spin.
    h_orb, _ = heat_at(ORB, 100)
    core = mix(REACTOR, THINK, h_orb)
    light = mix(GLOW, (255, 226, 170), h_orb)
    ox, oy = ORB
    f = ORB_R / 62

    def ring(r: float, color, width: float, a0: float = 0, a1: float = 360) -> None:
        box_ = P(ox - r * f, oy - r * f) + P(ox + r * f, oy + r * f)
        if a1 - a0 >= 360:
            d.ellipse(box_, outline=color, width=round(width * f * S))
        else:
            d.arc(box_, a0, a1, fill=color, width=round(width * f * S))

    pulse = 0.5 + 0.5 * math.sin(2 * math.pi * t * 3)
    dot(g, ORB, ORB_R + 22, mix((0, 0, 0), core, 0.3 + 0.35 * h_orb))
    spin = t * 360
    for k in range(4):                                   # HUD arcs around the disc, turning the other way
        a0 = -spin + k * 90
        ring(72, mix(BG, core, 0.75), 1.6, a0, a0 + 52)
    d.ellipse(P(ox - ORB_R, oy - ORB_R) + P(ox + ORB_R, oy + ORB_R), fill=(4, 16, 28))
    ring(58, core, 3)
    seg = 25
    for k in range(seg):
        a0 = spin + k * 360 / seg
        ring(48, mix(DEEP, core, 0.9), 10, a0, a0 + 360 / seg * 0.72)
    ring(40, light, 2.5)
    for k in range(14, 0, -1):                           # the core: a radial gradient deep -> reactor -> glow
        r = 37 * k / 14
        c = mix(light, core, k / 9) if k <= 9 else mix(core, DEEP, (k - 9) / 5)
        dot(d, ORB, r * f, c)
    dot(g, ORB, 30 * f, mix((0, 0, 0), light, 0.45 + 0.35 * pulse))

    def tri_pts(pts):
        return [P(ox + x * f, oy + y * f) for x, y in pts]
    outer = tri_pts([(-34, -19), (34, -19), (0, 36)])
    inner = tri_pts([(-20, -11), (20, -11), (0, 22)])
    d.polygon(outer, fill=DEEP)
    d.line([*outer, outer[0]], fill=(4, 16, 28), width=round(5 * f * S), joint="curve")
    d.line([*outer, outer[0]], fill=light, width=round(3 * f * S), joint="curve")
    d.line([*inner, inner[0]], fill=light, width=round(3 * f * S), joint="curve")
    mid = P(ox, oy + 1 * f)
    d.line([inner[0], mid, inner[1]], fill=light, width=round(3 * f * S), joint="curve")
    d.line([mid, inner[2]], fill=light, width=round(3 * f * S))
    text_c(d, (ox, oy + ORB_R + 21), "Джарвис решает", font(11, True), mix(MUTED, THINK, h_orb))

    glow = glow.filter(ImageFilter.GaussianBlur(9 * S))
    img = ImageChops.add(img, glow)
    return img.resize((W, H), Image.LANCZOS)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("Нужен ffmpeg")
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(FRAMES):
            frame(i).save(Path(tmp) / f"f{i:04d}.png")
        src = str(Path(tmp) / "f%04d.png")
        palette = str(Path(tmp) / "palette.png")
        subprocess.run([ffmpeg, "-y", "-v", "error", "-framerate", str(FPS), "-i", src,
                        "-vf", "palettegen=max_colors=96:stats_mode=full", palette], check=True)
        subprocess.run([ffmpeg, "-y", "-v", "error", "-framerate", str(FPS), "-i", src, "-i", palette,
                        "-lavfi", "paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle", "-loop", "0", str(OUT)],
                       check=True)
    print(f"{OUT} — {OUT.stat().st_size / 1024:.0f} КБ, {FRAMES} кадров")


if __name__ == "__main__":
    main()
