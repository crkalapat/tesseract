"""Earcons, synthesized at startup (no audio files to ship, instant playback)."""

from __future__ import annotations

import numpy as np

from .audio import HW_RATE


def _tone(freq: float, dur: float, decay: float = 8.0, attack: float = 0.004) -> np.ndarray:
    t = np.arange(int(HW_RATE * dur)) / HW_RATE
    env = np.exp(-decay * t) * np.clip(t / attack, 0, 1)
    # a touch of 2nd/3rd harmonic makes a small speaker sound like a bell, not a beep
    wave = np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(4 * np.pi * freq * t) + 0.08 * np.sin(6 * np.pi * freq * t)
    return (wave * env).astype(np.float32)


def _seq(notes: list[tuple[float, float, float]], total: float | None = None) -> np.ndarray:
    """notes = [(start_s, freq, dur)]"""
    end = total or max(s + d for s, _, d in notes)
    out = np.zeros(int(HW_RATE * end) + 1, dtype=np.float32)
    for start, f, d in notes:
        x = _tone(f, d)
        i = int(start * HW_RATE)
        out[i:i + len(x)] += x[: len(out) - i]
    peak = np.abs(out).max() or 1
    return out / peak * 0.6


class Sounds:
    def __init__(self):
        self.wake = _seq([(0, 659.3, 0.18), (0.07, 987.8, 0.25)])           # E5 -> B5
        self.error = _seq([(0, 440.0, 0.25), (0.12, 329.6, 0.35)])          # A4 -> E4
        self.agent_done = _seq([(0, 523.3, 0.3), (0.09, 659.3, 0.3), (0.18, 784.0, 0.45)])
        self.tick = _seq([(0, 1046.5, 0.05)])
        self.mute_on = _seq([(0, 784.0, 0.12), (0.08, 523.3, 0.2)])
        self.mute_off = _seq([(0, 523.3, 0.12), (0.08, 784.0, 0.2)])
        self.alarm = _seq([(0, 880, 0.12), (0.18, 880, 0.12), (0.36, 880, 0.12), (0.54, 880, 0.12)],
                          total=1.4)
        self.timer = _seq([(0, 1174.7, 0.15), (0.2, 1568.0, 0.25)], total=1.0)
