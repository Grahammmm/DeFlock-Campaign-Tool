"""Reel v2: narrated, subtitled, 3D-shot Reel.

Timeline is driven by the narration: each beat lasts as long as its spoken line.
Visuals come from the 3D shot library (Blender), the 2D map, or a fallback clip.
Every frame gets the same grade (vignette + fine grain), the brand bar with
deflockslo.com, word-by-word subtitles, and beat-specific graphics.
"""
import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import sfx
from .narration import narrate
from .render import (FPS, H, SAFE_BOTTOM, SAFE_X0, SAFE_X1, W, block_height, count_text, draw_lines, fit, font,
                     split_number, wrap)
from .shots import FRAME_SCENES, OPENERS, ensure_shot, shot_params

GAP = 0.28          # breath after each spoken line


# ---------------------------------------------------------------- planning
def beat_scene(beat, fact, index, day_seed):
    if beat.get("scene"):
        return beat["scene"]
    kind = beat["type"]
    if kind == "hook":
        return OPENERS[day_seed % 2]
    if kind == "map":
        return "county_pins"
    if kind == "stat":
        scene = FRAME_SCENES.get(fact.frame)
        if scene == "retention_blocks":
            n = split_number(beat.get("big", ""))
            # One block per day: only when the figure really is a count of days (not "260×").
            is_days = beat.get("small", "").lower().startswith("day") and not beat.get("big", "").endswith(("×", "x", "%"))
            return scene if n and n[1] >= 30 and is_days else OPENERS[(day_seed + 1) % 2]
        if scene == "network_arcs":
            n = split_number(beat.get("big", ""))
            return scene if n and 20 <= n[1] <= 400 else "county_pins"
        return scene or OPENERS[(day_seed + index) % 2]
    if kind == "cta":
        return "county_pins"
    return OPENERS[(day_seed + index) % 2]


def spoken(beat, campaign):
    if beat.get("say"):
        return beat["say"]
    kind = beat["type"]
    if kind == "stat":
        return f"{beat['big']}. {beat['small']}."
    if kind == "map":
        return f"{beat['big']}. {beat['small']}."
    if kind == "quote":
        return f"The records say: {beat['text']}"
    if kind == "cta":
        return campaign["cta"].get("say", beat["text"] + ". Sources at " + campaign.get("site", "") + ".")
    return beat["text"]


# ---------------------------------------------------------------- drawing helpers
def shadowed(img, draw_fn, radius=10, strength=170):
    """Draw text twice: a blurred dark copy for legibility over 3D footage, then the text."""
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(shadow), (0, 0, 0, strength))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(radius)))
    draw_fn(ImageDraw.Draw(img), None)


def grade_layers():
    yy, xx = np.mgrid[0:H, 0:W]
    d = np.sqrt(((xx - W / 2) / (W * 0.62)) ** 2 + ((yy - H / 2) / (H * 0.62)) ** 2)
    vignette = np.clip(1.08 - 0.55 * d ** 2, 0.45, 1.0).astype(np.float32)[..., None]
    rng = np.random.default_rng(3)
    grain = [(rng.standard_normal((H // 2, W // 2)).astype(np.float32) * 3.0) for _ in range(6)]
    grain = [np.kron(g, np.ones((2, 2), dtype=np.float32))[..., None] for g in grain]
    return vignette, grain


class Reel:
    def __init__(self, brand, campaign, map_data=None, library=None, log=print):
        self.b = brand
        self.c = campaign
        self.map_data = map_data
        self.library = library
        self.log = log
        self.site = campaign.get("site", "")

    # ---- per-beat overlays (static parts cached, animated parts per frame)
    def chrome(self):
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle((SAFE_X0, 232, SAFE_X1, 238), fill=self.b.cream + (60,))
        shadowed(img, lambda dd, sh: (dd.text((SAFE_X0, 256), self.b.name, font=font("Bold", 38), fill=sh or self.b.cream),
                                      dd.text((SAFE_X0, 302), self.site, font=font("SemiBold", 30), fill=sh or self.b.mint)),
                 radius=6, strength=150)
        note = f"3D illustration · sources at {self.site}" if self.site else "3D illustration"
        fn = font("Regular", 26)
        shadowed(img, lambda dd, sh: dd.text((SAFE_X1 - fn.getlength(note), SAFE_BOTTOM), note, font=fn,
                                             fill=sh or self.b.cream + (200,)), radius=5, strength=170)
        return img

    def karaoke(self, text, words, t, y=None):
        """Large centered statement whose words light up as they are spoken (the captions for sound-off viewers).

        Spoken words are cream, the current word mint, upcoming words faint, so the whole line is readable
        at a glance and the eye follows the voice.
        """
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        f, lines = fit(text, "Bold", SAFE_X1 - SAFE_X0, 88, 58, 6)
        idx = sum(1 for w in words if w[1] <= t) - 1
        h = block_height(lines, f)
        y = y if y is not None else int(760 - h / 2)
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw_lines(ImageDraw.Draw(shadow), lines, f, y, (0, 0, 0, 200))
        img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)))
        d = ImageDraw.Draw(img)
        k, yy = 0, y
        for line in lines:
            x = (W - f.getlength(line)) / 2
            for part in line.split():
                color = self.b.mint + (255,) if k == idx else self.b.cream + ((255,) if k < idx else (80,))
                d.text((x, yy), part, font=f, fill=color)
                x += f.getlength(part + " ")
                k += 1
            yy += f.size * 1.18
        return img

    def stat(self, beat, t):
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        big = count_text(beat["big"], min(1, t / 1.0))
        fb = font("Bold", 230 if len(beat["big"]) <= 6 else 170 if len(beat["big"]) <= 9 else 124)
        fs, lines = fit(beat["small"], "SemiBold", SAFE_X1 - SAFE_X0, 54, 40, 4)
        y = 470
        wb = fb.getlength(big)
        shadowed(img, lambda dd, sh: dd.text(((W - wb) / 2, y), big, font=fb, fill=sh or self.b.mint), radius=16, strength=210)
        y2 = y + fb.size * 1.08
        shadowed(img, lambda dd, sh: draw_lines(dd, lines, fs, y2, sh or self.b.cream), radius=10, strength=200)
        if beat.get("source"):
            y3 = y2 + block_height(lines, fs) + 18
            fsrc, sl = fit("Source: " + beat["source"], "Regular", SAFE_X1 - SAFE_X0, 30, 26, 2)
            shadowed(img, lambda dd, sh: draw_lines(dd, sl, fsrc, y3, sh or self.b.cream), radius=6, strength=190)
        return img

    def quote(self, beat, t):
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        f, lines = fit("“" + beat["text"] + "”", "SemiBold", SAFE_X1 - SAFE_X0 - 60, 60, 42, 6)
        h = block_height(lines, f)
        y = 560
        slide = int(60 * (1 - min(1, t / 0.35)) ** 2)
        d.rounded_rectangle((SAFE_X0 - 30, y - 70 + slide, SAFE_X1 + 30, y + h + 120 + slide), 18, fill=self.b.cream + (246,))
        d.rectangle((SAFE_X0 - 30, y - 70 + slide, SAFE_X0 - 16, y + h + 120 + slide), fill=self.b.coral)
        d.text((SAFE_X0 + 10, y - 52 + slide), "FROM THE RECORDS", font=font("Bold", 26), fill=self.b.coral)
        yy = draw_lines(d, lines, f, y + slide, self.b.ink, align="left", x0=SAFE_X0 + 10)
        if beat.get("source"):
            fsrc, sl = fit(beat["source"], "Regular", SAFE_X1 - SAFE_X0 - 40, 28, 24, 2)
            draw_lines(d, sl, fsrc, yy + 22, self.b.muted, align="left", x0=SAFE_X0 + 10)
        return img

    def map_label(self, beat, t, dur):
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        n = split_number(beat["big"])
        prog = min(1, t / (dur * 0.7))
        label = count_text(beat["big"], prog) if n else beat["big"]
        fb = font("Bold", 200)
        wb = fb.getlength(label)
        shadowed(img, lambda dd, sh: dd.text(((W - wb) / 2, 360), label, font=fb, fill=sh or self.b.cream), radius=16, strength=200)
        fs, lines = fit(beat["small"], "SemiBold", SAFE_X1 - SAFE_X0, 46, 36, 3)
        shadowed(img, lambda dd, sh: draw_lines(dd, lines, fs, 590, sh or self.b.mint), radius=8, strength=200)
        return img

    def end_card(self, beat, t):
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        pop = 1 - (1 - min(1, t / 0.3)) ** 3
        f, lines = fit(beat["text"], "Bold", SAFE_X1 - SAFE_X0 - 60, 76, 54, 3)
        h = block_height(lines, f)
        y = 520 + int((1 - pop) * 80)
        d.rounded_rectangle((SAFE_X0 - 30, y - 60, SAFE_X1 + 30, y + h + 250), 30, fill=self.b.coral + (int(242 * pop),))
        yy = draw_lines(d, lines, f, y, self.b.cream + (int(255 * pop),))
        fsite = font("Bold", 84)
        ws = fsite.getlength(self.site)
        d.text(((W - ws) / 2, yy + 30), self.site, font=fsite, fill=self.b.cream + (int(255 * pop),))
        fsm = font("SemiBold", 38)
        small = beat.get("small", "")
        d.text(((W - fsm.getlength(small)) / 2, yy + 140), small, font=fsm, fill=self.b.cream + (int(230 * pop),))
        return img

    # ---- build
    def build(self, script, fact, out, day_seed=0, offline_voice=False, fallback_clip=None, ai_background=False):
        work = Path(tempfile.mkdtemp())
        beats = script["beats"]
        timeline, t0 = [], 0.0
        voice_used = None
        for i, beat in enumerate(beats):
            say = spoken(beat, self.c)
            wav = work / f"n{i}.wav"
            secs, provider = narrate(say, wav, self.c.get("voice", {}), offline=offline_voice, log=self.log)
            voice_used = voice_used or provider
            dur = max(secs + GAP, 2.2 if beat["type"] != "cta" else 3.4)
            shown = beat.get("text", "") if beat["type"] in ("hook", "line") else ""
            words = timed_words(shown, 0.08, secs) if shown else []
            scene = beat_scene(beat, fact, i, day_seed)
            timeline.append({"beat": beat, "say": say, "wav": wav, "start": t0, "dur": dur, "voice": secs,
                             "words": words, "scene": scene})
            t0 += dur
        total = t0
        # Footage per beat: resolve shots now (renders missing ones), decode lazily per beat.
        for seg in timeline:
            seg["clip"] = self.shot_for(seg, fact) or fallback_clip
            seg["ai"] = bool(seg["clip"] and seg["clip"] == fallback_clip and ai_background)
        # Audio
        mix = sfx.Mix(total)
        mix.add(sfx.room_tone(total), 0, 1.0)
        for i, seg in enumerate(timeline):
            mix.add_wav(seg["wav"], seg["start"] + 0.08, 1.0)
            if i:
                mix.add(sfx.whoosh(), seg["start"] - 0.18, 0.35)
            if seg["beat"]["type"] in ("stat", "map"):
                mix.add(sfx.hit(), seg["start"] + 0.02, 0.55)
                for k in range(8):
                    mix.add(sfx.tick(), seg["start"] + 0.1 + k * 0.1, 0.5)
            if seg["scene"] == "plate_scan":
                mix.add(sfx.scan_beep(), seg["start"] + seg["dur"] * 0.45, 0.6)
        if len(timeline) > 1:
            mix.add(sfx.riser(0.8), timeline[1]["start"] - 0.8, 0.25)
        mix.add(sfx.hit(1.0), timeline[-1]["start"], 0.6)
        audio = work / "mix.wav"
        mix.write(audio, total)
        # Video
        vignette, grain = grade_layers()
        chrome = as_layer(self.chrome())
        enc = subprocess.Popen([
            "ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
            "-r", str(FPS), "-i", "-", "-i", str(audio), "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-maxrate", "12M", "-bufsize", "24M", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)
        frames = int(total * FPS)
        cache = {}
        prev = None
        current, fr = None, None
        ai_note = self.ai_note()
        for n in range(frames):
            t = n / FPS
            i = max(k for k, s in enumerate(timeline) if s["start"] <= t + 1e-6)
            seg = timeline[i]
            if current != i:
                current, fr = i, footage(seg["clip"], int(seg["dur"] * FPS) + 2)
            bt = t - seg["start"]
            # Slow push-in on every shot (also scales the 720p shot up to 1080x1920).
            base = push_in(fr[min(len(fr) - 1, int(bt * FPS))], 1.0 + 0.05 * bt / seg["dur"])
            beat = seg["beat"]
            kind = beat["type"]
            if kind != "hook":
                base *= 0.72  # darken under graphics
            if kind in ("hook", "line"):
                idx = sum(1 for w in seg["words"] if w[1] <= bt)
                layer = cache.get((i, idx))
                if layer is None:
                    cache.clear()
                    layer = cache[(i, idx)] = as_layer(self.karaoke(beat["text"], seg["words"], bt,
                                                                    520 if kind == "hook" else None))
            else:
                done = {"stat": 1.05, "map": seg["dur"] * 0.72, "quote": 0.4, "cta": 0.35}[kind]
                key = (i, "done")
                if bt >= done and key in cache:
                    layer = cache[key]
                else:
                    tt = bt if bt < done else 99
                    img = {"stat": lambda: self.stat(beat, tt), "map": lambda: self.map_label(beat, tt, seg["dur"]),
                           "quote": lambda: self.quote(beat, tt), "cta": lambda: self.end_card(beat, tt)}[kind]()
                    layer = as_layer(img)
                    if bt >= done:
                        cache.clear()
                        cache[key] = layer
            frame = base * layer[1] + layer[0]
            frame = frame * chrome[1] + chrome[0]
            if seg["ai"]:
                frame = frame * ai_note[1] + ai_note[0]
            frame[232:239, SAFE_X0:SAFE_X0 + int((SAFE_X1 - SAFE_X0) * min(1, t / total))] = self.b.mint
            frame = frame * vignette + grain[n % len(grain)]
            if bt < 3 / FPS and prev is not None:  # 3-frame dissolve between beats
                w = 1 - bt * FPS / 3
                frame = frame * (1 - w) + prev * w
            if bt < 2 / FPS and n > 0:
                frame = frame + 18 * (1 - bt * FPS / 2)  # tiny flash on cut
            out_frame = frame.clip(0, 255).astype(np.uint8)
            enc.stdin.write(out_frame.tobytes())
            if i + 1 < len(timeline) and t + 1 / FPS >= timeline[i + 1]["start"]:
                prev = frame
        enc.stdin.close()
        if enc.wait() != 0:
            raise RuntimeError("ffmpeg encode failed")
        return {"seconds": round(total, 1), "voice": voice_used,
                "scenes": [s["scene"] for s in timeline], "beats": len(timeline)}

    def shot_for(self, seg, fact):
        """The beat's 3D shot from the library (rendered if missing and Blender is available), else None."""
        if not self.library:
            return None
        parts = split_number(seg["beat"].get("big", ""))
        if seg["scene"] == "county_pins" and not self.map_data:
            return None
        params = shot_params(seg["scene"], fact, self.map_data, parts[1] if parts else None)
        try:
            return ensure_shot(seg["scene"], params, self.library, self.log)
        except Exception as exc:
            self.log(f"3D shot {seg['scene']} failed ({exc}); using fallback")
            return None

    def ai_note(self):
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        shadowed(img, lambda dd, sh: dd.text((SAFE_X0, SAFE_BOTTOM - 40), "Background video is AI-generated",
                                             font=font("Regular", 26), fill=sh or self.b.cream), radius=5, strength=170)
        return as_layer(img)


def as_layer(img):
    """RGBA image -> (premultiplied RGB, 1 - alpha) so compositing is one multiply-add per frame."""
    o = np.asarray(img, dtype=np.float32)
    a = o[..., 3:4] / 255
    return o[..., :3] * a, 1 - a


def timed_words(text, start, seconds):
    """Spread words across the spoken duration, weighted by length (+ pauses at punctuation)."""
    words = text.split()
    weights = [len(re.sub(r"\W", "", w)) + 2 + (3 if w.endswith((".", ",", ":", "?", "!")) else 0) for w in words]
    total = sum(weights) or 1
    out, t = [], start
    for w, wt in zip(words, weights):
        out.append((w, t))
        t += seconds * wt / total
    return out


def footage(clip, need):
    """`need` frames for a beat at FPS, native resolution (scaled to 9:16 later). Slows short shots to fit."""
    if not clip:
        return np.full((1, H // 2, W // 2, 3), 12, dtype=np.uint8)
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(clip), "-t", "8",
                          "-vf", f"scale=-2:1280,crop=720:1280,fps={FPS}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         check=True, capture_output=True).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 1280, 720, 3)
    if len(frames) >= need:
        return frames[:need]
    return frames[np.linspace(0, len(frames) - 1, need).astype(int)]


def push_in(frame, zoom):
    """Center-crop by `zoom` and resize to the output size."""
    h, w = frame.shape[:2]
    ch, cw = h / zoom, w / zoom
    y0, x0 = (h - ch) / 2, (w - cw) / 2
    img = Image.fromarray(frame).resize((W, H), Image.BICUBIC, box=(x0, y0, x0 + cw, y0 + ch))
    return np.asarray(img, dtype=np.float32)
