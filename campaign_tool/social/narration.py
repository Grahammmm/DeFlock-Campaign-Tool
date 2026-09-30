"""Voice narration for the Reel: one audio clip per beat, so beat timing follows the voice.

Providers, in order of preference:
- ElevenLabs (ELEVENLABS_API_KEY and a voice id) for the most natural or cloned voice;
- Gemini TTS (GEMINI_API_KEY), the same key as Veo;
- espeak-ng, offline and robotic, for layout tests only;
- silence with an estimated duration, if nothing else is available.
"""
import json
import os
import re
import shutil
import subprocess
import urllib.request
import wave
from pathlib import Path

GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
STYLE = "Read this like a calm, serious local documentary narrator. Clear, steady, not dramatic: "


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def to_wav(src, dst):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "44100", str(dst)], check=True)


def elevenlabs(text, out, voice_id, key):
    body = {"text": text, "model_id": os.environ.get("SOCIAL_ELEVENLABS_MODEL", "eleven_multilingual_v2"),
            "voice_settings": {"stability": 0.55, "similarity_boost": 0.8, "style": 0.15, "use_speaker_boost": True}}
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128",
        data=json.dumps(body).encode(), headers={"xi-api-key": key, "content-type": "application/json"})
    mp3 = Path(str(out) + ".mp3")
    with urllib.request.urlopen(req, timeout=120) as resp:
        mp3.write_bytes(resp.read())
    to_wav(mp3, out)
    mp3.unlink()


def gemini(text, out, voice, key):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=os.environ.get("SOCIAL_TTS_MODEL", GEMINI_TTS_MODEL),
        contents=STYLE + text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)))))
    pcm = resp.candidates[0].content.parts[0].inline_data.data
    raw = Path(str(out) + ".raw.wav")
    with wave.open(str(raw), "wb") as w:  # Gemini returns 24 kHz 16-bit mono PCM
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    to_wav(raw, out)
    raw.unlink()


def espeak(text, out):
    tmp = Path(str(out) + ".es.wav")
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "165", "-w", str(tmp), text], check=True)
    to_wav(tmp, out)
    tmp.unlink()


def silence(text, out):
    seconds = max(1.2, len(text.split()) / 2.6)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", f"{seconds:.2f}", str(out)], check=True)


def narrate(text, out, voice_cfg, offline=False, log=print):
    """Write narration for `text` to `out` (44.1 kHz mono WAV). Returns (seconds, provider)."""
    out = Path(out)
    # Subtitles show the written text; the voice gets spoken forms ("deflockslo.com" -> "deflock S L O dot com").
    for written, said in sorted((voice_cfg.get("pronounce") or {}).items(), key=lambda kv: -len(kv[0])):
        text = re.sub(r"(?<![\w.])" + re.escape(written) + r"(?![\w])", said, text)
    attempts = []
    if not offline:
        if os.environ.get("ELEVENLABS_API_KEY") and (voice_cfg.get("elevenlabs_voice_id") or os.environ.get("SOCIAL_ELEVENLABS_VOICE")):
            attempts.append(("elevenlabs", lambda: elevenlabs(
                text, out, os.environ.get("SOCIAL_ELEVENLABS_VOICE") or voice_cfg["elevenlabs_voice_id"],
                os.environ["ELEVENLABS_API_KEY"])))
        if os.environ.get("GEMINI_API_KEY"):
            attempts.append(("gemini", lambda: gemini(text, out, voice_cfg.get("gemini_voice", "Charon"),
                                                       os.environ["GEMINI_API_KEY"])))
    if shutil.which("espeak-ng"):
        attempts.append(("espeak", lambda: espeak(text, out)))
    attempts.append(("silence", lambda: silence(text, out)))
    for name, fn in attempts:
        try:
            fn()
            return duration(out), name
        except Exception as exc:
            log(f"narration via {name} failed ({type(exc).__name__}: {exc}); trying next")
    raise RuntimeError("no narration provider worked")
