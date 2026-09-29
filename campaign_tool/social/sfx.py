"""Sound design synthesized from scratch (no licensed samples): whooshes, hits, ticks, a riser and room tone.

Everything is generated with NumPy at 44.1 kHz, so there are no rights questions and
nothing to download. Music is left out on purpose: add a track in the Instagram app.
"""
import wave

import numpy as np

SR = 44100
RNG = np.random.default_rng(7)


def _env(n, attack=0.01, release=0.3):
    t = np.linspace(0, 1, n)
    a = np.clip(t / max(attack, 1e-4), 0, 1)
    r = np.clip((1 - t) / max(release, 1e-4), 0, 1)
    return a * r


def _lowpass(x, alpha):
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc += alpha * (v - acc)
        y[i] = acc
    return y


def whoosh(seconds=0.45):
    n = int(SR * seconds)
    noise = RNG.standard_normal(n)
    # Sweep a one-pole low-pass open then closed for the "air" movement.
    sweep = np.sin(np.linspace(0, np.pi, n)) ** 2
    out = np.empty(n)
    acc = 0.0
    for i in range(n):
        acc += (0.02 + 0.35 * sweep[i]) * (noise[i] - acc)
        out[i] = acc
    return out * sweep * 0.9


def hit(seconds=0.7):
    n = int(SR * seconds)
    t = np.arange(n) / SR
    freq = 55 + 60 * np.exp(-t * 18)
    body = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t * 5.5)
    click = RNG.standard_normal(n) * np.exp(-t * 90) * 0.25
    return (body + click) * 0.9


def tick(seconds=0.03):
    n = int(SR * seconds)
    t = np.arange(n) / SR
    return np.sin(2 * np.pi * 2400 * t) * np.exp(-t * 180) * 0.25


def riser(seconds=0.9):
    n = int(SR * seconds)
    t = np.linspace(0, 1, n)
    noise = _lowpass(RNG.standard_normal(n), 0.08)
    tone = np.sin(2 * np.pi * np.cumsum(180 + 520 * t ** 2) / SR)
    return (noise * 0.5 + tone * 0.18) * t ** 2


def scan_beep(seconds=0.22):
    n = int(SR * seconds)
    t = np.arange(n) / SR
    return (np.sin(2 * np.pi * 1320 * t) + 0.4 * np.sin(2 * np.pi * 2640 * t)) * _env(n, 0.02, 0.5) * 0.18


def room_tone(seconds):
    n = int(SR * seconds)
    t = np.arange(n) / SR
    drone = 0.5 * np.sin(2 * np.pi * 55 * t) + 0.3 * np.sin(2 * np.pi * 82.4 * t + 1) + 0.2 * np.sin(2 * np.pi * 110 * t)
    wobble = 0.75 + 0.25 * np.sin(2 * np.pi * 0.13 * t)
    air = _lowpass(RNG.standard_normal(n), 0.02) * 0.6
    fade = np.minimum(1, np.minimum(t / 1.5, (seconds - t) / 1.2))
    return (drone * wobble + air) * np.clip(fade, 0, 1) * 0.08


class Mix:
    def __init__(self, seconds):
        self.buf = np.zeros(int(SR * (seconds + 1)))

    def add(self, sound, at, gain=1.0):
        i = int(at * SR)
        if i >= len(self.buf):
            return
        j = min(len(self.buf), i + len(sound))
        self.buf[i:j] += sound[: j - i] * gain

    def add_wav(self, path, at, gain=1.0):
        with wave.open(str(path)) as w:
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64) / 32768
        self.add(data, at, gain)

    def write(self, path, seconds):
        x = self.buf[: int(SR * seconds)]
        peak = np.max(np.abs(x)) or 1
        x = np.tanh(x / peak * 1.2) * 0.89  # gentle limiter
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((x * 32767).astype(np.int16).tobytes())
