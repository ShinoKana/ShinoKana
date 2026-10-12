#!/usr/bin/env python3
"""Turn a short clip into the looping header (assets/header[-dark].avif / .webp).

    python3 scripts/header.py clip.mp4 --en-font GreatVibes-Regular.ttf --outdir assets \
        --format avif --width 1600          # sharp version for retina screens
    python3 scripts/header.py clip.mp4 --en-font GreatVibes-Regular.ttf --outdir assets \
        --format webp --width 960           # fallback for browsers without AVIF

Each loop of the clip, 見知らぬわたし is written onto the typewriter paper
stroke by stroke, then "Kana" is signed under it; both fade before the loop
restarts. The ink is multiplied into the paper and tilted with it, and its
colour drifts through colours picked from the picture. The dark variant is
graded down afterwards (the art is very bright for a dark page) and its
edges fade into GitHub's dark background.

Stroke order comes from KanjiVG (assets/kanjivg, CC BY-SA 3.0, Ulrich Apel,
https://kanjivg.tagaini.net). Needs ffmpeg (with libsvtav1 for AVIF), img2webp or ffmpeg libwebp_anim for
WebP, Pillow and
numpy. Script font: github.com/google/fonts ofl/greatvibes (its K stays a K
at this size; Pinyon Script's reads as H).
"""
import argparse
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
JP_TEXT = "見知らぬわたし"
EN_TEXT = "Kana"
FPS = 30
FADE = 0.5  # cross-fade between the clip's tail and head

# Placement as fractions of the frame. The paper's top edge rises about 3
# degrees to the right and the sheet leans back, so the ink does too.
JP_CENTER, JP_SIZE = (0.725, 0.262), 0.048
EN_BASELINE_RIGHT, EN_SIZE = (0.975, 0.372), 0.036  # right of the quill, which ends near x=0.925
TILT, SQUASH = 2.5, 0.93
JP_ITALIC = 0.21  # horizontal shear, about 12 degrees

# Timeline within one loop, in seconds (the loop is the clip minus FADE).
WRITE_START, WRITE_TIME = 0.3, 2.0
SIGN_GAP, SIGN_TIME = 0.15, 0.6
FADE_OUT = 0.6  # ends 0.15 s before the loop restarts

# ink bottle, ribbon blue, book lavender, star charm pink
PALETTE = ["#446997", "#5f86b3", "#7a72a6", "#b9788a"]
DARK_GRADE = ("curves=master='0/0 0.25/0.13 0.5/0.33 0.75/0.53 1/0.72',eq=saturation=1.12,"
              "colorbalance=bs=0.04:bm=0.03:bh=0.02")
PAGES = {"light": ("#ffffff", 0.0), "dark": ("#0d1117", 0.022)}  # background, edge feather


# ---------------------------------------------------------------- strokes


def parse_path(d):
    """KanjiVG path data (M, C, c, S, s) -> list of points, cubics sampled."""
    tokens = re.findall(r"[MmCcSsLl]|-?(?:\d+\.?\d*|\.\d+)", d)
    pts, i, cmd = [], 0, None
    cur = (0.0, 0.0)
    last_ctrl = None

    def num():
        nonlocal i
        i += 1
        return float(tokens[i - 1])

    def cubic(p0, p1, p2, p3):
        for k in range(1, 17):
            t = k / 16
            a, b, c, e = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
            pts.append((a * p0[0] + b * p1[0] + c * p2[0] + e * p3[0],
                        a * p0[1] + b * p1[1] + c * p2[1] + e * p3[1]))

    while i < len(tokens):
        if re.match(r"[A-Za-z]", tokens[i]):
            cmd = tokens[i]
            i += 1
        rel = cmd.islower()
        ox, oy = cur if rel else (0.0, 0.0)
        if cmd in "Mm":
            cur = (ox + num(), oy + num())
            pts.append(cur)
            last_ctrl = None
        elif cmd in "Ll":
            cur = (ox + num(), oy + num())
            pts.append(cur)
            last_ctrl = None
        elif cmd in "Cc":
            p1 = (ox + num(), oy + num())
            p2 = (ox + num(), oy + num())
            p3 = (ox + num(), oy + num())
            cubic(cur, p1, p2, p3)
            cur, last_ctrl = p3, p2
        elif cmd in "Ss":
            p1 = (2 * cur[0] - last_ctrl[0], 2 * cur[1] - last_ctrl[1]) if last_ctrl else cur
            p2 = (ox + num(), oy + num())
            p3 = (ox + num(), oy + num())
            cubic(cur, p1, p2, p3)
            cur, last_ctrl = p3, p2
    return pts


def load_strokes(ch):
    svg = (ROOT / "assets" / "kanjivg" / f"{ord(ch):05x}.svg").read_text()
    paths = re.findall(r'<path id="kvg:[0-9a-f]+-s(\d+)"[^>]*\sd="([^"]+)"', svg)
    return [parse_path(d) for _, d in sorted(paths, key=lambda p: int(p[0]))]


def length(pts):
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def partial(pts, frac):
    """The first `frac` of a polyline, by length."""
    if frac >= 1:
        return pts
    target, out = length(pts) * frac, [pts[0]]
    for a, b in zip(pts, pts[1:]):
        step = math.dist(a, b)
        if step >= target:
            k = target / step if step else 0
            out.append((a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k))
            return out
        out.append(b)
        target -= step
    return out


class Writer:
    """Stroke-by-stroke 見知らぬわたし: schedule + mask rendering."""

    STROKE_PAUSE, CHAR_PAUSE = 0.02, 0.06

    def __init__(self):
        self.chars = [load_strokes(ch) for ch in JP_TEXT]
        ink = sum(length(s) for c in self.chars for s in c)
        pauses = sum(len(c) - 1 for c in self.chars) * self.STROKE_PAUSE + (len(self.chars) - 1) * self.CHAR_PAUSE
        speed = ink / (WRITE_TIME - pauses)  # KanjiVG units per second
        self.schedule, t = [], WRITE_START
        for ci, strokes in enumerate(self.chars):
            for si, s in enumerate(strokes):
                dur = length(s) / speed
                self.schedule.append((ci, si, t, dur))
                t += dur + self.STROKE_PAUSE
            t += self.CHAR_PAUSE - self.STROKE_PAUSE

    def mask(self, t, size_px, ss=3):
        box = size_px * ss
        advance = box * 1.02
        W, H = round(advance * len(self.chars) + box * 0.2), round(box * 1.2)
        im = Image.new("L", (W, H), 0)
        draw = ImageDraw.Draw(im)
        width = max(1, round(box * 5.2 / 109))
        for ci, si, start, dur in self.schedule:
            if t <= start:
                continue
            pts = partial(self.chars[ci][si], (t - start) / dur)
            k = box / 109
            ox, oy = box * 0.1 + ci * advance, box * 0.1
            xy = [(ox + x * k, oy + y * k) for x, y in pts]
            if len(xy) > 1:
                draw.line(xy, fill=255, width=width, joint="curve")
            for x, y in (xy[0], xy[-1]):
                draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=255)
        return im.resize((W // ss, H // ss), Image.LANCZOS)


def signature(font, t, ss=3):
    """'Kana' revealed left to right along the script's slant."""
    big = font.font_variant(size=font.size * ss)
    l, top, r, b = big.getbbox(EN_TEXT, anchor="ls")
    pad = big.size // 2
    im = Image.new("L", (r - l + 2 * pad, b - top + 2 * pad), 0)
    ImageDraw.Draw(im).text((pad - l, pad - top), EN_TEXT, font=big, fill=255, anchor="ls", stroke_width=1, stroke_fill=255)
    im = im.resize((im.width // ss, im.height // ss), Image.LANCZOS)
    W, H = im.size
    pad, l, top = pad // ss, l / ss, top / ss
    start = WRITE_START + WRITE_TIME + SIGN_GAP
    p = min(max((t - start) / SIGN_TIME, 0), 1)
    p = 1 - (1 - p) ** 1.6  # the pen slows down at the end
    soft, slant = font.size * 0.25, 0.35
    edge = -soft - slant * H + p * (W + 2 * soft + slant * H)
    ys, xs = np.mgrid[0:H, 0:W]
    reveal = np.clip((edge + slant * (H - ys) - xs) / soft + 0.5, 0, 1)
    return Image.fromarray((np.asarray(im) * reveal).astype(np.uint8)), (pad - l, pad - top)


def place(mask, anchor_in_mask, at, frame_size, italic=0.0):
    """Slant, squash, tilt and paste `mask` so anchor_in_mask lands on `at`."""
    ax, ay = anchor_in_mask
    if italic:
        w, h = mask.size
        mask = mask.transform((w + round(italic * h), h), Image.AFFINE,
                              (1, italic, -italic * h, 0, 1, 0), resample=Image.BICUBIC)
        ax += italic * (h - ay)
    w, h = mask.size
    mask = mask.resize((w, max(1, round(h * SQUASH))), Image.LANCZOS)
    ay *= SQUASH
    big = Image.new("L", frame_size, 0)
    big.paste(mask, (round(at[0] - ax), round(at[1] - ay)))
    return big.rotate(TILT, resample=Image.BICUBIC, center=at)


# ---------------------------------------------------------------- colour


def rgb(h):
    return np.array([int(h[i : i + 2], 16) for i in (1, 3, 5)], dtype=np.float32)


def cycle(phase):
    n = len(PALETTE)
    p = (phase % 1) * n
    i, f = int(p), p - int(p)
    f = (1 - math.cos(f * math.pi)) / 2
    return rgb(PALETTE[i]) * (1 - f) + rgb(PALETTE[(i + 1) % n]) * f


def ink(frame, mask, phase, opacity):
    """Multiply a left-to-right two-colour gradient into the frame through mask."""
    a = np.asarray(mask, dtype=np.float32)[..., None] / 255 * opacity
    if not a.any():
        return frame
    cols = np.nonzero(a[..., 0].any(axis=0))[0]
    x0, x1 = cols[0], cols[-1] + 1
    k = np.clip((np.arange(frame.shape[1]) - x0) / max(1, x1 - x0), 0, 1)[None, :, None]
    color = cycle(phase) * (1 - k) + cycle(phase + 0.5) * k
    return frame * (1 - a + a * color / 255)


# ---------------------------------------------------------------- main


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def edge_mask(size, feather):
    W, H = size
    f = round(feather * H)
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle((f, f, W - 1 - f, H - 1 - f), radius=round(H * 0.035), fill=255)
    return m.filter(ImageFilter.GaussianBlur(f * 0.6)) if f else m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip")
    ap.add_argument("--en-font", required=True)
    ap.add_argument("--outdir", default=str(ROOT / "assets"))
    ap.add_argument("--width", type=int, default=960)
    ap.add_argument("--format", choices=("avif", "webp"), default="webp")
    ap.add_argument("--quality", type=int, default=75, help="WebP quality")
    ap.add_argument("--crf", type=int, default=30, help="AVIF quality (lower is better)")
    ap.add_argument("--still", type=float, metavar="SECONDS",
                    help="only write header[-dark]-still.png at this time, for layout checks")
    args = ap.parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    dur = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", args.clip], text=True))
    tmp = Path(tempfile.mkdtemp())
    try:
        raw = tmp / "raw"
        raw.mkdir()
        run(["ffmpeg", "-y", "-i", args.clip, "-filter_complex",
             f"[0:v]fps={FPS},scale={args.width}:-2:flags=lanczos,split[a][b];"
             f"[a]trim={FADE}:{dur},setpts=PTS-STARTPTS[main];"
             f"[b]trim=0:{FADE},setpts=PTS-STARTPTS[head];"
             f"[main][head]xfade=transition=fade:duration={FADE}:offset={dur - 2 * FADE}",
             str(raw / "%04d.png")])
        frames = sorted(raw.glob("*.png"))
        loop = len(frames) / FPS
        if args.still is not None:
            frames = [frames[min(len(frames) - 1, round(args.still * FPS))]]
        size = W, H = Image.open(frames[0]).size

        writer = Writer()
        en_font = ImageFont.truetype(args.en_font, round(H * EN_SIZE))
        fade_end = loop - 0.15
        for path in frames:
            t = int(path.stem) / FPS - 1 / FPS
            opacity = 0.92 * min(1, max(0, (fade_end - t) / FADE_OUT))
            phase = t / loop
            frame = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
            jp = writer.mask(t, H * JP_SIZE)
            jp = place(jp, (jp.size[0] / 2, jp.size[1] / 2), (W * JP_CENTER[0], H * JP_CENTER[1]), size, JP_ITALIC)
            frame = ink(frame, jp, phase, opacity)
            en, (bx, by) = signature(en_font, t)
            r = W * EN_BASELINE_RIGHT[0] - (en.size[0] - bx - en_font.size // 2)
            en = place(en, (bx, by), (r, H * EN_BASELINE_RIGHT[1]), size)
            frame = ink(frame, en, phase + 0.25, min(1, opacity / 0.92))
            Image.fromarray(frame.clip(0, 255).astype(np.uint8)).save(path)

        for variant, (page, feather) in PAGES.items():
            src = raw
            if variant == "dark":
                src = tmp / "dark"
                src.mkdir()
                first = int(frames[0].stem)
                run(["ffmpeg", "-y", "-start_number", str(first), "-i", str(raw / "%04d.png"),
                     "-frames:v", str(len(frames)), "-vf", DARK_GRADE, "-start_number", str(first),
                     str(src / "%04d.png")])
            mask = edge_mask(size, feather)
            bg = Image.new("RGB", size, page)
            final = tmp / f"final-{variant}"
            final.mkdir()
            for path in frames:
                Image.composite(Image.open(src / path.name).convert("RGB"), bg, mask).save(final / path.name)
            name = "header" + ("" if variant == "light" else "-dark")
            if args.still is not None:
                shutil.copy(final / frames[0].name, outdir / f"{name}-still.png")
                continue
            out = outdir / f"{name}.{args.format}"
            if args.format == "avif":
                run(["ffmpeg", "-y", "-framerate", str(FPS), "-i", str(final / "%04d.png"),
                     "-c:v", "libsvtav1", "-preset", "5", "-crf", str(args.crf), "-pix_fmt", "yuv420p",
                     "-g", str(FPS * 2), "-loop", "0", str(out)])
            elif shutil.which("img2webp"):
                run(["img2webp", "-loop", "0", "-lossy", "-q", str(args.quality), "-m", "5",
                     "-d", str(round(1000 / FPS)), *map(str, sorted(final.glob("*.png"))), "-o", str(out)])
            else:
                run(["ffmpeg", "-y", "-framerate", str(FPS), "-i", str(final / "%04d.png"),
                     "-c:v", "libwebp_anim", "-lossy", "1", "-quality", str(args.quality), "-loop", "0", str(out)])
            print(f"{out}: {len(frames)} frames, {W}x{H}, {out.stat().st_size / 1e6:.2f} MB")
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
