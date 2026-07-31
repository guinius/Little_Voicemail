#!/usr/bin/env python3
"""Generate the built-in ringtones.

Kept in the repo so the sounds can be regenerated or tweaked rather than
being opaque binaries. Run from the project root:

    python3 tools/make_ringtones.py

Parents can also just drop their own .wav/.mp3/.ogg files into sounds/ and
pick them from the web UI.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 44100
AMPLITUDE = 0.35

# Note frequencies, equal temperament.
NOTES = {
    "C5": 523.25, "D5": 587.33, "E5": 659.25, "F5": 698.46,
    "G5": 783.99, "A5": 880.00, "B5": 987.77, "C6": 1046.50,
    "E6": 1318.51, "G6": 1567.98, "A4": 440.00, "C4": 261.63,
    "E4": 329.63, "G4": 392.00,
}

# (name, [(note, seconds), ...]) - short, bright and non-alarming.
TUNES = {
    "chime": [("E5", 0.18), ("G5", 0.18), ("C6", 0.42)],
    "doorbell": [("E5", 0.32), ("C5", 0.55)],
    "bubbles": [("C5", 0.09), ("E5", 0.09), ("G5", 0.09), ("C6", 0.09), ("G5", 0.26)],
    "twinkle": [("C6", 0.12), ("A5", 0.12), ("F5", 0.12), ("C6", 0.34)],
    "hello": [("G4", 0.16), ("C5", 0.16), ("E5", 0.16), ("G5", 0.40)],
}


def envelope(index: int, total: int) -> float:
    """Fade in and out so notes do not click."""
    attack = int(SAMPLE_RATE * 0.008)
    release = int(total * 0.55)
    if index < attack:
        return index / attack
    if index > total - release:
        return max(0.0, (total - index) / release)
    return 1.0


def render(tune: list[tuple[str, float]]) -> bytes:
    frames = bytearray()
    for note, seconds in tune:
        freq = NOTES[note]
        count = int(SAMPLE_RATE * seconds)
        for i in range(count):
            t = i / SAMPLE_RATE
            # Fundamental plus a soft second harmonic for a bell-like tone.
            value = math.sin(2 * math.pi * freq * t)
            value += 0.28 * math.sin(4 * math.pi * freq * t)
            value *= AMPLITUDE * envelope(i, count) / 1.28
            frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))
    return bytes(frames)


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "sounds"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, tune in TUNES.items():
        path = out_dir / f"{name}.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(render(tune))
        print(f"wrote {path.relative_to(path.parent.parent)}")


if __name__ == "__main__":
    main()
