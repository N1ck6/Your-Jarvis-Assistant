"""Short synthesized cue sounds, no audio files needed."""
from __future__ import annotations

import numpy as np

from assistant.audio.player import AudioClip

SR = 48000


def _tone(freqs: list[float], dur: float, gap: float = 0.0) -> np.ndarray:
    parts = []
    for f in freqs:
        t = np.arange(int(SR * dur)) / SR
        env = np.minimum(1.0, t / 0.01) * np.exp(-t * 7)
        wave = 0.6 * np.sin(2 * np.pi * f * t) + 0.25 * np.sin(4 * np.pi * f * t)
        parts.append((wave * env).astype(np.float32))
        if gap:
            parts.append(np.zeros(int(SR * gap), dtype=np.float32))
    return np.concatenate(parts)


WAKE = AudioClip(_tone([660, 990], 0.09), SR)
DONE = AudioClip(_tone([880, 660], 0.08), SR)
ERROR = AudioClip(_tone([330, 247], 0.14), SR)
ALARM = AudioClip(np.concatenate([_tone([988, 1319], 0.12, 0.05)] * 3), SR)
