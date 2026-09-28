"""Background clips from Google Veo 3.1, with a local library so clips are reused.

Each clip is keyed by a hash of its prompt and model. A cached clip is never paid
for twice. Without GEMINI_API_KEY (or with --stub) a plain animated placeholder is
made locally so the rest of the pipeline can run and be tested offline.
"""
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

DEFAULT_MODEL = "veo-3.1-fast-generate-preview"


def clip_key(prompt, model):
    return hashlib.sha256(f"{model}\n{prompt}".encode()).hexdigest()[:20]


def get_clip(prompt, library, negative="", model=None, stub=False, log=print):
    """Return (path, info) for a 9:16 8-second clip matching `prompt`."""
    model = model or os.environ.get("SOCIAL_VEO_MODEL", DEFAULT_MODEL)
    library = Path(library)
    library.mkdir(parents=True, exist_ok=True)
    key = clip_key(prompt, "stub" if stub else model)
    path = library / f"{key}.mp4"
    meta = library / f"{key}.json"
    if path.exists() and path.stat().st_size > 10_000:
        log(f"clip: reused {path.name}")
        return path, json.loads(meta.read_text()) if meta.exists() else {"key": key}
    if stub or not os.environ.get("GEMINI_API_KEY"):
        make_placeholder(path)
        info = {"key": key, "model": "placeholder", "prompt": prompt, "ai_generated": False}
    else:
        info = generate_with_veo(prompt, negative, model, path, log)
    meta.write_text(json.dumps(info, indent=2))
    return path, info


def generate_with_veo(prompt, negative, model, path, log):
    from google import genai  # imported lazily: optional dependency
    from google.genai import types
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.GenerateVideosConfig(aspect_ratio="9:16", resolution="1080p", duration_seconds=8,
                                        number_of_videos=1, negative_prompt=negative or None)
    started = time.time()
    op = client.models.generate_videos(model=model, prompt=prompt, config=config)
    while not op.done:
        if time.time() - started > 900:
            raise TimeoutError("Veo generation took longer than 15 minutes")
        time.sleep(10)
        op = client.operations.get(op)
    if getattr(op, "error", None):
        raise RuntimeError(f"Veo error: {op.error}")
    videos = op.response.generated_videos if op.response else []
    if not videos:
        # Usually a safety filter; the caller falls back to a library clip.
        raise RuntimeError("Veo returned no video (possibly filtered)")
    client.files.download(file=videos[0].video)
    videos[0].video.save(str(path))
    seconds = round(time.time() - started)
    log(f"clip: generated with {model} in {seconds}s")
    return {"key": path.stem, "model": model, "prompt": prompt, "negative": negative,
            "ai_generated": True, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "seconds": seconds}


def make_placeholder(path, seconds=8):
    """A dark moving-noise clip so layouts can be reviewed without Veo."""
    subprocess.run([
        "ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", f"color=c=0x0d3b3e:s=1080x1920:d={seconds}:r=30",
        "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=brown:a=0.02",
        "-vf", "noise=alls=18:allf=t+u,gblur=sigma=6,eq=brightness=-0.05",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True)


def library_fallback(library, exclude=None):
    """Any previously generated Veo clip, for days when generation fails."""
    for meta in sorted(Path(library).glob("*.json")):
        info = json.loads(meta.read_text())
        clip = meta.with_suffix(".mp4")
        if info.get("ai_generated") and clip.exists() and clip != exclude:
            return clip, info
    return None, None
