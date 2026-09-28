"""Render a script into a 1080x1920 Reel: background clip, animated text beats, map reveal, CTA.

Frames are composed with Pillow/NumPy and piped to ffmpeg. Text sits inside
Instagram's safe area (clear of the top bar and the bottom caption area).
"""
import json
import math
import re
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1080, 1920, 30
SAFE_X0, SAFE_X1, SAFE_TOP, SAFE_BOTTOM = 90, 990, 300, 1440
FONTS = Path(__file__).parent / "fonts"
DURATIONS = {"hook": 2.8, "stat": 4.2, "line": 3.4, "quote": 4.4, "map": 4.8, "cta": 3.6}


def hex_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def font(weight, size):
    return ImageFont.truetype(str(FONTS / f"IBMPlexSans-{weight}.ttf"), size)


def wrap(text, fnt, width):
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if fnt.getlength(trial) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def fit(text, weight, width, max_size, min_size, max_lines):
    size = max_size
    while size > min_size:
        f = font(weight, size)
        lines = wrap(text, f, width)
        if len(lines) <= max_lines:
            return f, lines
        size -= 4
    f = font(weight, min_size)
    return f, wrap(text, f, width)


class Brand:
    def __init__(self, cfg):
        self.ink = hex_rgb(cfg.get("ink", "#092b2d"))
        self.cream = hex_rgb(cfg.get("cream", "#fff8ec"))
        self.coral = hex_rgb(cfg.get("coral", "#b83d2c"))
        self.mint = hex_rgb(cfg.get("mint", "#9edfd3"))
        self.muted = hex_rgb(cfg.get("muted", "#4d6060"))
        self.name = cfg.get("name", "")
        self.handle = cfg.get("handle", "")


def draw_lines(draw, lines, fnt, y, fill, align="center", spacing=1.18, x0=SAFE_X0, x1=SAFE_X1):
    size = fnt.size
    for line in lines:
        width = fnt.getlength(line)
        x = x0 if align == "left" else (x0 + x1 - width) / 2
        draw.text((x, y), line, font=fnt, fill=fill)
        y += size * spacing
    return y


def block_height(lines, fnt, spacing=1.18):
    return len(lines) * fnt.size * spacing


def split_number(big):
    """'$152,299' -> ('$', 152299, '', True); returns None if not a clean number."""
    m = re.fullmatch(r"([^\d]*)([\d,]+)([^\d]*)", big.strip())
    if not m:
        return None
    return m.group(1), int(m.group(2).replace(",", "")), m.group(3), "," in m.group(2)


def count_text(big, progress):
    parts = split_number(big)
    if not parts or progress >= 1:
        return big
    pre, value, post, commas = parts
    shown = int(value * (1 - (1 - progress) ** 3))
    return f"{pre}{shown:,}{post}" if commas else f"{pre}{shown}{post}"


class Renderer:
    def __init__(self, brand, map_data=None):
        self.b = brand
        self.map_data = map_data

    # ---- overlays -------------------------------------------------------------------------
    def overlay(self, beat, t, dur):
        """RGBA overlay for a beat at time t (seconds into the beat)."""
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        kind = beat["type"]
        if kind == "hook":
            f, lines = fit(beat["text"], "Bold", SAFE_X1 - SAFE_X0, 96, 60, 5)
            h = block_height(lines, f)
            y = 820 - h / 2
            d.rounded_rectangle((SAFE_X0 - 36, y - 44, SAFE_X1 + 36, y + h + 30), 28, fill=self.b.ink + (215,))
            draw_lines(d, lines, f, y, self.b.cream)
        elif kind == "stat":
            big = count_text(beat["big"], min(1, t / 0.9))
            fb = font("Bold", 230 if len(beat["big"]) <= 6 else 170 if len(beat["big"]) <= 9 else 120)
            fs, lines = fit(beat["small"], "SemiBold", SAFE_X1 - SAFE_X0, 60, 42, 4)
            h = fb.size * 1.05 + 30 + block_height(lines, fs)
            y = 820 - h / 2
            d.rounded_rectangle((SAFE_X0 - 36, y - 50, SAFE_X1 + 36, y + h + (190 if beat.get("source") else 60)), 28, fill=self.b.ink + (225,))
            wb = fb.getlength(big)
            d.text(((W - wb) / 2, y - fb.size * 0.12), big, font=fb, fill=self.b.mint)
            y = draw_lines(d, lines, fs, y + fb.size * 1.05 + 30, self.b.cream)
            self.source(d, beat.get("source", ""), y + 30)
        elif kind == "line":
            f, lines = fit(beat["text"], "Bold", SAFE_X1 - SAFE_X0, 80, 52, 6)
            h = block_height(lines, f)
            y = 820 - h / 2
            d.rounded_rectangle((SAFE_X0 - 36, y - 44, SAFE_X1 + 36, y + h + 30), 28, fill=self.b.ink + (215,))
            draw_lines(d, lines, f, y, self.b.cream)
        elif kind == "quote":
            f, lines = fit("“" + beat["text"] + "”", "SemiBold", SAFE_X1 - SAFE_X0 - 40, 64, 44, 7)
            h = block_height(lines, f)
            y = 800 - h / 2
            d.rounded_rectangle((SAFE_X0 - 36, y - 60, SAFE_X1 + 36, y + h + 130), 24, fill=self.b.cream + (245,))
            d.rectangle((SAFE_X0 - 36, y - 60, SAFE_X0 - 20, y + h + 130), fill=self.b.coral)
            y = draw_lines(d, lines, f, y, self.b.ink, align="left", x0=SAFE_X0 + 10)
            self.source(d, beat.get("source", ""), y + 30, fill=self.b.muted)
        elif kind == "cta":
            f, lines = fit(beat["text"], "Bold", SAFE_X1 - SAFE_X0 - 40, 84, 56, 4)
            h = block_height(lines, f)
            y = 780 - h / 2
            d.rounded_rectangle((SAFE_X0 - 36, y - 60, SAFE_X1 + 36, y + h + 150), 32, fill=self.b.coral + (240,))
            y = draw_lines(d, lines, f, y, self.b.cream)
            fs = font("SemiBold", 46)
            draw_lines(d, [beat.get("small", "")], fs, y + 28, self.b.cream)
        return img

    def source(self, d, text, y, fill=None):
        if not text:
            return
        f, lines = fit("Source: " + text, "Regular", SAFE_X1 - SAFE_X0, 36, 30, 3)
        draw_lines(d, lines, f, y, fill or self.b.mint)

    def chrome(self, ai_background):
        """Brand tag, progress track and the AI-background note (static parts)."""
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle((SAFE_X0, 232, SAFE_X1, 238), fill=self.b.cream + (70,))
        f = font("Bold", 40)
        d.text((SAFE_X0, 256), self.b.name, font=f, fill=self.b.cream)
        if ai_background:
            fs = font("Regular", 28)
            d.text((SAFE_X0, SAFE_BOTTOM + 12), "Background video is AI-generated. Facts are from public records.", font=fs,
                   fill=self.b.cream + (200,))
        return img

    # ---- map scene -------------------------------------------------------------------------
    def map_frame(self, beat, t, dur):
        img = Image.new("RGB", (W, H), self.b.ink)
        if not self.map_data:
            return img
        d = ImageDraw.Draw(img, "RGBA")
        proj = self.map_data["project"]
        for ring in self.map_data["boundary"]:
            d.line([proj(*p) for p in ring], fill=self.b.mint + (160,), width=4)
        for line in self.map_data["roads"]:
            d.line([proj(*p) for p in line], fill=self.b.cream + (60,), width=3)
        pts = self.map_data["points"]
        shown = int(len(pts) * min(1, max(0, (t - 0.3) / (dur * 0.6))))
        for i, (lon, lat) in enumerate(pts[:shown]):
            x, y = proj(lon, lat)
            age = t - 0.3 - i / len(pts) * dur * 0.6
            r = 11 + (6 * max(0, 1 - age * 4))
            d.ellipse((x - r, y - r, x + r, y + r), fill=self.b.coral + (235,), outline=self.b.cream + (180,), width=2)
        fb = font("Bold", 170)
        label = count_text(beat["big"], min(1, shown / max(1, len(pts))))
        d.text(((W - fb.getlength(label)) / 2, 300), label, font=fb, fill=self.b.cream)
        fs, lines = fit(beat["small"], "SemiBold", SAFE_X1 - SAFE_X0, 48, 36, 3)
        draw_lines(d, lines, fs, 500, self.b.mint)
        return img

    # ---- compose ---------------------------------------------------------------------------
    def render(self, script, clip, out, ai_background, audio_volume=0.35, log=print):
        beats = script["beats"]
        durations = [DURATIONS.get(b["type"], 3.5) for b in beats]
        total = sum(durations)
        frames = int(total * FPS)
        bg = decode_frames(clip)
        shade = np.linspace(0.62, 0.45, H, dtype=np.float32)[:, None, None]
        enc = subprocess.Popen([
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
            "-stream_loop", "-1", "-i", str(clip),
            "-map", "0:v", "-map", "1:a?", "-af", f"volume={audio_volume},afade=t=out:st={total - 1:.2f}:d=1",
            "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-t", f"{total:.2f}", "-movflags", "+faststart", str(out),
        ], stdin=subprocess.PIPE)
        starts = np.cumsum([0] + durations)
        cache = {}
        ch = np.asarray(self.chrome(ai_background), dtype=np.float32)
        chrome_rgb, chrome_a = ch[..., :3], ch[..., 3:4] / 255.0
        for n in range(frames):
            t = n / FPS
            i = int(np.searchsorted(starts, t, side="right") - 1)
            i = min(i, len(beats) - 1)
            beat, bt, dur = beats[i], t - starts[i], durations[i]
            if beat["type"] == "map":
                base = np.asarray(self.map_frame(beat, bt, dur), dtype=np.float32)
            else:
                base = bg[n % len(bg)].astype(np.float32) * shade
            animated = beat["type"] == "stat" and bt < 1.0
            key = (i, "anim", n) if animated else (i, "static")
            if beat["type"] == "map":
                over = None
            elif key in cache:
                over = cache[key]
            else:
                over = np.asarray(self.overlay(beat, bt, dur), dtype=np.float32)
                if not animated:
                    cache[key] = over
            frame = base
            if over is not None:
                a = min(1.0, bt / 0.25) * min(1.0, (dur - bt) / 0.2 + 0.001)
                lift = (1 - min(1.0, bt / 0.25)) * 40
                if lift:
                    over = np.roll(over, int(lift), axis=0)
                alpha = over[..., 3:4] / 255.0 * a
                frame = frame * (1 - alpha) + over[..., :3] * alpha
            frame = frame * (1 - chrome_a) + chrome_rgb * chrome_a
            frame[232:239, SAFE_X0:SAFE_X0 + int((SAFE_X1 - SAFE_X0) * min(1, t / total))] = self.b.mint
            enc.stdin.write(frame.clip(0, 255).astype(np.uint8).tobytes())
        enc.stdin.close()
        if enc.wait() != 0:
            raise RuntimeError("ffmpeg encode failed")
        return {"seconds": round(total, 1), "frames": frames}


def decode_frames(clip, max_seconds=8):
    """Decode a clip to 1080x1920 RGB frames (scaled to cover and center-cropped)."""
    raw = subprocess.run([
        "ffmpeg", "-loglevel", "error", "-i", str(clip), "-t", str(max_seconds),
        "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], check=True, capture_output=True).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W, 3)
    if len(frames) == 0:
        raise RuntimeError(f"No frames decoded from {clip}")
    # Ping-pong loop so a short clip never jumps back to its first frame.
    return np.concatenate([frames, frames[::-1]])


def load_map(boundary_path, points_path, roads_path=None):
    """County outline, point locations and optional roads, with a projection into the frame."""
    boundary = json.loads(Path(boundary_path).read_text())
    rings = []
    for feat in boundary["features"]:
        geom = feat["geometry"]
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        for poly in polys:
            rings.append([tuple(p[:2]) for p in poly[0]])
    points = [tuple(f["geometry"]["coordinates"][:2]) for f in json.loads(Path(points_path).read_text())["features"]
              if f["geometry"]["type"] == "Point"]
    points.sort(key=lambda p: (-p[1], p[0]))  # reveal north to south
    roads = []
    if roads_path and Path(roads_path).exists():
        for el in json.loads(Path(roads_path).read_text()).get("elements", []):
            roads.append([(g["lon"], g["lat"]) for g in el.get("geometry", [])])
    lons = [p[0] for r in rings for p in r]
    lats = [p[1] for r in rings for p in r]
    k = math.cos(math.radians(sum(lats) / len(lats)))
    x0, x1, y0, y1 = min(lons), max(lons), min(lats), max(lats)
    box = (60, 620, 1020, 1470)
    scale = min((box[2] - box[0]) / ((x1 - x0) * k), (box[3] - box[1]) / (y1 - y0))
    ox = box[0] + ((box[2] - box[0]) - (x1 - x0) * k * scale) / 2
    oy = box[1] + ((box[3] - box[1]) - (y1 - y0) * scale) / 2

    def project(lon, lat):
        return ox + (lon - x0) * k * scale, oy + (y1 - lat) * scale

    return {"boundary": rings, "points": points, "roads": roads, "project": project}
