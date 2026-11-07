"""Small, allocation-light DSP helpers (numpy only, no scipy on the Pi)."""

from __future__ import annotations

import math

import numpy as np


def _kaiser_sinc(n_taps: int, cutoff: float, beta: float = 8.0) -> np.ndarray:
    """Windowed-sinc lowpass. cutoff is in cycles/sample (0..0.5)."""
    t = np.arange(n_taps) - (n_taps - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * t) * np.kaiser(n_taps, beta)
    return (h / h.sum()).astype(np.float32)


class StreamResampler:
    """Streaming rational resampler (polyphase FIR), e.g. 48k->16k or 24k->48k.

    Feed it chunks of any size; it returns as many output samples as the input
    so far allows and carries filter state between calls, so chunk boundaries
    are seamless.
    """

    def __init__(self, in_rate: int, out_rate: int, taps_per_phase: int = 16):
        g = math.gcd(in_rate, out_rate)
        self.up, self.down = out_rate // g, in_rate // g
        if self.up == self.down == 1:
            self.K = 1
            self.H = np.ones((1, 1), dtype=np.float32)
        else:
            self.K = int(math.ceil(taps_per_phase * max(1.0, self.down / self.up)))
            proto = _kaiser_sinc(self.up * self.K, 0.45 / max(self.up, self.down)) * self.up
            # H[p, k] = h[p + up*k]
            self.H = proto.reshape(self.K, self.up).T.copy()
        self._k = np.arange(self.K)
        self.reset()

    def reset(self) -> None:
        self.buf = np.zeros(self.K - 1, dtype=np.float32)
        self.buf_start = -(self.K - 1)  # absolute input index of buf[0]
        self.n_next = 0  # next output sample index

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.up == self.down == 1:
            return x.astype(np.float32, copy=False)
        self.buf = np.concatenate((self.buf, x.astype(np.float32, copy=False)))
        total_in = self.buf_start + len(self.buf)
        n_end = (total_in * self.up - 1) // self.down + 1
        if n_end <= self.n_next:
            return np.zeros(0, dtype=np.float32)
        m = np.arange(self.n_next, n_end, dtype=np.int64) * self.down
        base, phase = m // self.up, m % self.up
        idx = base[:, None] - self._k[None, :] - self.buf_start
        y = np.einsum("nk,nk->n", self.buf[idx], self.H[phase])
        self.n_next = n_end
        keep_from = (n_end * self.down) // self.up - self.K + 1 - self.buf_start
        if keep_from > 0:
            self.buf = self.buf[keep_from:]
            self.buf_start += keep_from
        return y.astype(np.float32, copy=False)


class Varispeed:
    """Plays a stream a hair faster or slower, seamlessly across chunks, to keep two clocks in step.
    step = input samples per output sample: 1.001 is 0.1% faster (1.7 cents, far below hearing).
    Linear interpolation, which is plenty that close to 1; at exactly 1 it passes samples through."""

    def __init__(self):
        self.prev: np.ndarray | None = None          # the last input sample: index 0 of the next chunk
        self.t = 0.0                                 # where the next output falls, from prev

    def process(self, x: np.ndarray, step: float = 1.0) -> np.ndarray:
        x = x.astype(np.float32, copy=False)
        if self.prev is None:
            if not len(x):
                return x
            self.prev, x = x[:1], x[1:]
        buf = np.concatenate((self.prev, x))
        last = len(buf) - 1
        if step == 1.0:
            self.t = float(round(self.t))            # back in step: whole samples again, no smoothing
        n = int((last - self.t) // step) + 1 if self.t <= last else 0
        y = np.interp(self.t + step * np.arange(n), np.arange(len(buf)), buf).astype(np.float32)
        self.t += step * n - last
        self.prev = buf[-1:]
        return y


class DCBlocker:
    """Removes the MEMS mic's DC offset with a slow block-wise running mean."""

    def __init__(self, alpha: float = 0.02):
        self.alpha = alpha
        self.dc = None

    def process(self, x: np.ndarray) -> np.ndarray:
        m = float(x.mean())
        self.dc = m if self.dc is None else self.dc + self.alpha * (m - self.dc)
        return x - self.dc


def float_to_int16(x: np.ndarray) -> np.ndarray:
    return (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16)


def rms(x: np.ndarray) -> float:
    if len(x) == 0:
        return 0.0
    x = x.astype(np.float32, copy=False)
    return float(np.sqrt(np.mean(x * x)))


def db_to_gain(db: float) -> float:
    return 10 ** (db / 20)


def volume_to_gain(level: int, max_db: float = 0.0) -> float:
    """0..10 volume steps -> linear gain. ~3.5 dB per step, 0 = silent."""
    if level <= 0:
        return 0.0
    return db_to_gain(max_db - (10 - min(level, 10)) * 3.5)


def level_to_unit(rms_value: float, floor_db: float = -55, ceil_db: float = -12) -> float:
    """Map an RMS amplitude (0..1 full scale) onto 0..1 for LED/visual meters."""
    if rms_value <= 0:
        return 0.0
    db = 20 * math.log10(rms_value)
    return min(1.0, max(0.0, (db - floor_db) / (ceil_db - floor_db)))
