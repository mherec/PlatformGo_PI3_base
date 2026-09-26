"""Generates small test WAV files (16-bit mono 22.05 kHz) on first start."""
import math
import struct
import wave
from pathlib import Path

RATE = 22050


def _tone(freqs, dur, vol=0.5, fade=0.01):
    """freqs: callable t -> Hz, or a constant."""
    n = int(RATE * dur)
    phase, out = 0.0, bytearray()
    for i in range(n):
        t = i / RATE
        f = freqs(t) if callable(freqs) else freqs
        phase += 2 * math.pi * f / RATE
        env = min(1.0, t / fade, (dur - t) / fade)
        out += struct.pack("<h", int(32767 * vol * env * math.sin(phase)))
    return bytes(out)


SOUNDS = {
    "beep": lambda: _tone(1000, 0.25),
    "chime": lambda: _tone(880, 0.18) + _tone(0, 0.04, 0) + _tone(1320, 0.3),
    "error": lambda: _tone(220, 0.2) + _tone(0, 0.08, 0) + _tone(220, 0.35),
    "sweep": lambda: _tone(lambda t: 200 * (10 ** (t / 2)), 2.0, 0.4),   # 200 Hz -> 2 kHz
    "melody": lambda: b"".join(_tone(f, 0.16, 0.4) for f in (523, 659, 784, 1047, 784, 659, 523)),
}


def ensure(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    for name, gen in SOUNDS.items():
        path = directory / f"{name}.wav"
        if path.exists():
            continue
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(gen())
