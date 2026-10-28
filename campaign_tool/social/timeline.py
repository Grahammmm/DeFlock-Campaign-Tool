"""Dependency-free planning helpers for the narrated Reel.

Scene choice, spoken lines, word timing and narration-provider bookkeeping live
here so they can be imported and tested without NumPy, Pillow or ffmpeg. The
renderer (`reel.py`) imports them; only drawing and encoding need the media
dependencies.
"""
import re

from .shots import FRAME_SCENES, OPENERS

# Narration providers that are fine to post (an AI label is still required) and the
# test fallbacks that must never be posted.
AI_VOICES = ("elevenlabs", "gemini")
TEST_VOICES = ("espeak", "silence")


def split_number(big):
    """'$152,299' -> ('$', 152299, '', True); returns None if not a clean number."""
    m = re.fullmatch(r"([^\d]*)([\d,]+)([^\d]*)", big.strip())
    if not m:
        return None
    return m.group(1), int(m.group(2).replace(",", "")), m.group(3), "," in m.group(2)


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


def voice_summary(voices):
    """Summarise per-beat narration providers for the render metadata.

    Returns the single provider to report as `voice`: a test fallback (silence before
    espeak) if ANY beat used one, so a later fallback can never be hidden behind an
    earlier AI voice; otherwise the first provider used; None when there were no beats.
    """
    for test in ("silence", "espeak"):
        if test in voices:
            return test
    return voices[0] if voices else None


def render_voices(render):
    """Per-beat providers from render metadata (older metadata only has `voice`)."""
    render = render or {}
    voices = list(render.get("voices") or [])
    if not voices and render.get("voice"):
        voices = [render["voice"]]
    return voices
