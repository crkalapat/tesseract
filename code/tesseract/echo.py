"""Acoustic echo cancellation: takes Franklin's own voice back out of the mic.

The speaker sits a couple of centimetres from the mic, inside the same box, so while Franklin talks
the mic hears mostly Franklin, maybe 20-30 dB louder than you from across the room. The wake word
can't pick "Franklin" out of that, which is why it couldn't be interrupted.

But we know exactly what the speaker is playing (the mixer wrote it), and the mic and the amp share
one I2S clock, so their sample counters never drift apart. That makes this the easy case of echo
cancellation:

  1. Line up: find how many samples after the mixer writes a sample its echo shows up in the mic
     (cross-correlation, GCC-PHAT). With a shared clock this stays put until the card restarts or
     runs dry, so it's checked now and then and re-found if it moves.
  2. Linear filter: a partitioned-block frequency-domain adaptive filter (120 ms long) learns the
     speaker -> box -> mic path and subtracts its prediction of the echo. It slows its learning
     while you talk over it ("double talk"), so it doesn't learn your voice away.
  3. Residual suppression: a tiny speaker driven hard isn't linear, so some echo is left. Where the
     predicted echo dominates a frequency band, that band is turned down; where you dominate, it's
     left alone.

When nothing is playing, the reference is silence, the prediction is zero and the mic passes
through unchanged (20 ms later, for the suppressor's overlap-add).
"""

from __future__ import annotations

import collections
import logging
import threading

import numpy as np

log = logging.getLogger(__name__)

RATE = 16_000
B = 320                  # block = one 20 ms mic frame
N = 2 * B                # FFT size
TAPS = 6                 # partitions: 6 x 20 ms = a 120 ms echo path
RING = 4 * RATE          # 4 s of history for both signals
MARGIN = 48              # start the filter 3 ms before the echo's first arrival


class EchoCanceller:
    def __init__(self, suppress: float = 2.0, mu: float = 0.5):
        self.suppress = suppress
        self.mu = mu
        # the speaker thread only ever takes this small lock, for a few microseconds: it must never
        # wait on the cancelling itself, or the sound card runs dry and pops
        self._far_lock = threading.Lock()
        self._reset_pending = False
        self._far = np.zeros(RING, np.float32)     # what the speaker played, by absolute sample index
        self._mic = np.zeros(RING, np.float32)     # what the mic heard (before cancelling), likewise
        self.n_far = 0
        self.n_mic = 0
        self.delay: int | None = None              # mic index - far index of the same sound
        self._candidate: int | None = None
        self._next_check = 0
        self.converged = False
        self.leak = 1.0
        self._dt_frames = 0
        self._avg_far = 0.0
        self._worse = 0
        self._floor = 1e-6                          # the room's noise in a frame, tracked from below
        self.erle_db = 0.0                          # how much of its own voice it's removing, lately
        self.delay_ms = 0.0                         # speaker -> mic, after the mixer writes
        self.resyncs = 0
        self.tape: dict | None = None               # `tesseract interrupt-test` records everything here
        # per 20 ms frame: how much more the mic heard than its own echo explains (see someone_talked)
        self.unexplained: collections.deque = collections.deque(maxlen=500)   # (frame number, ratio), 10 s
        self._frame = 0
        self._last_loud = -10 * RATE
        self._win = np.sqrt(np.hanning(N + 1)[:N]).astype(np.float32)   # sqrt-Hann: perfect overlap-add
        self.reset()

    def reset(self) -> None:
        self.W = np.zeros((TAPS, B + 1), np.complex64)
        self.X = np.zeros((TAPS, B + 1), np.complex64)
        self._prev_far = np.zeros(B, np.float32)
        self._prev_e = np.zeros(B, np.float32)
        self._prev_y = np.zeros(B, np.float32)
        self._tail = np.zeros(B, np.float32)
        self.converged = False
        self._dt_frames = 0

    # ── the speaker side (mixer thread) ─────────────────────────────────────
    def far(self, x: np.ndarray) -> None:
        """16 kHz float audio, exactly as written to the sound card."""
        if len(x) and float(np.abs(x).max()) > 1e-3:
            self._last_loud = self.n_far + len(x)
        if self.tape is not None:
            self.tape["far"].append((self.n_far, x.copy()))
        with self._far_lock:
            self._put(self._far, self.n_far, x)
            self.n_far += len(x)

    def discontinuity(self) -> None:
        """The card was reopened or ran dry: the two clocks' line-up has to be found again (by the mic
        thread, at its next frame)."""
        self._reset_pending = True

    def playing_recently(self, seconds: float = 1.5) -> bool:
        """Has the speaker played anything (a reply, a chime, music) in the last `seconds`?"""
        return self.n_far - self._last_loud < seconds * RATE

    def someone_talked(self, seconds: float = 1.5, ratio: float = 4.0, frames: int = 3,
                       at: int | None = None) -> bool | None:
        """Did a person speak in the `seconds` up to mic frame `at` (default: the latest)? Meaning the mic
        heard >= `ratio` x what its own echo and the room's noise explain, for `frames` frames. When the
        wake word fires while Franklin talks, "no" means it heard itself. None: it can't tell (not lined
        up yet, or `at` is too long ago).

        `at` matters: the wake word runs a little behind the mic, and the check later still. Looking
        back from "now" missed a real "Franklin" on the Pi; looking back from the frame the wake word
        fired on doesn't."""
        if self.delay is None or not self.converged:
            return None
        entries = list(self.unexplained)
        if not entries:
            return None
        end = entries[-1][0] if at is None else at
        lo = end - int(seconds * 50)
        if entries[0][0] > lo + 1:
            return None
        return sum(r >= ratio for i, r in entries if lo < i <= end) >= frames

    def summary(self) -> str:
        if self.delay is None:
            return "echo cancelling: not lined up with the speaker yet (needs a second or two of speech)"
        return f"echo cancelling: removing {self.erle_db:.0f} dB of its own voice (speaker reaches the mic " \
               f"{self.delay_ms:.0f} ms after writing; re-synced {self.resyncs} times)"

    def _far_slice(self, start, length):
        with self._far_lock:
            if start + length > self.n_far or start < self.n_far - RING + length:
                return None                          # not written yet / too old
            return self._get(self._far, start, length)

    @staticmethod
    def _put(ring, n, x):
        i = n % RING
        k = min(len(x), RING - i)
        ring[i:i + k] = x[:k]
        ring[: len(x) - k] = x[k:]

    @staticmethod
    def _get(ring, start, length):
        idx = (np.arange(start, start + length) % RING)
        return ring[idx]

    # ── the mic side (capture thread) ───────────────────────────────────────
    def process(self, d: np.ndarray) -> np.ndarray:
        """One 320-sample mic frame (16 kHz float) in, the same frame with the echo removed out."""
        if self._reset_pending:
            self._reset_pending = False
            self.delay, self._candidate, self._next_check = None, None, self.n_mic
            self.resyncs += 1
            self.reset()
        if self.tape is not None:
            self.tape["mic"].append((self.n_mic, d.copy()))
        j = self.n_mic
        self._frame = j // B                        # = the frame's MicFrame.index once it's published
        self._put(self._mic, j, d)
        self.n_mic += B
        if j >= self._next_check:
            self._estimate_delay()
        if self.delay is None:
            out = self._suppress(d, np.zeros(B, np.float32))
        else:
            x = self._far_slice(j - self.delay + MARGIN, B)    # the speaker audio from just before its echo
            out = self._cancel(d, np.zeros(B, np.float32) if x is None else x)
        if self.tape is not None:
            self.tape["out"].append(out.copy())
        return out

    def _cancel(self, d, x):
        Xk = np.fft.rfft(np.concatenate((self._prev_far, x))).astype(np.complex64)
        self._prev_far = x
        self.X = np.roll(self.X, 1, axis=0)
        self.X[0] = Xk
        y = np.fft.irfft((self.W * self.X).sum(axis=0))[B:].astype(np.float32)
        e = d - y

        py, pe, pd = float(y @ y), float(e @ e), float(d @ d)
        self._floor += (0.2 if pe < self._floor else 0.002) * (pe - self._floor)
        self.unexplained.append((self._frame, pe / (self.leak * py + 3 * self._floor + 1e-9)))
        # watchdog: an output louder than the input for 100 ms means the filter went unstable: start over
        self._worse = self._worse + 1 if pe > 2 * pd + 1e-6 else 0
        if self._worse > 5:
            log.info("echo: the filter went unstable; starting it over")
            self.W[:] = 0
            self.converged, self.leak, self._worse = False, 1.0, 0
            return self._suppress(d, np.zeros(B, np.float32))
        far_energy = float((np.abs(self.X) ** 2).sum())
        self._avg_far += 0.02 * (far_energy - self._avg_far)
        # learn only while it's really talking, not from the echo's tail in the pauses (tested on a real
        # recording from the cube: learning in the tails made it diverge)
        if far_energy > 1e-3 and far_energy > 0.1 * self._avg_far:
            mu = self.mu
            # `leak`: the share of the echo the filter normally leaves behind. Much more left than that
            # means someone is talking over it: barely learn then, or it learns your voice away.
            if self.converged and pe > 4 * self.leak * py + 1e-6 * B:
                mu = 0.0
                self._dt_frames += 1
                if self._dt_frames > 100:            # 2 s of "talking over it" = the echo path moved: re-learn
                    self.converged, self.leak, self._dt_frames = False, 1.0, 0
            else:
                self._dt_frames = 0
                if py > 1e-6 * B:
                    self.leak += 0.1 * (min(1.0, pe / py) - self.leak)
                    self.converged = self.converged or self.leak < 0.25     # 6 dB: it has the echo path
                    if pd > 1e-6 * B and pe > 0:
                        self.erle_db += 0.05 * (10 * np.log10(pd / pe) - self.erle_db)
            Ek = np.fft.rfft(np.concatenate((np.zeros(B, np.float32), e)))
            power = (np.abs(self.X) ** 2).sum(axis=0)
            # regularized by the signal's own level: bands his voice barely uses would otherwise get huge,
            # noisy updates (with the cube's +23 dB speaker-to-mic gain, enough to blow the filter up)
            power = power + 0.1 * power.mean() + 1e-6 * N
            G = mu * np.conj(self.X) * (Ek / power)
            g = np.fft.irfft(G, axis=1)               # keep the filter causal (the "constrained" update)
            g[:, B:] = 0
            self.W += np.fft.rfft(g, axis=1).astype(np.complex64)
        return self._suppress(e, y)

    def _suppress(self, e, y):
        """Turn down the bands where the echo the filter left behind is louder than everything else."""
        E = np.fft.rfft(np.concatenate((self._prev_e, e)) * self._win)
        self._prev_e = e
        if self.suppress > 0 and float(y @ y) > 1e-7:
            Y = np.fft.rfft(np.concatenate((self._prev_y, y)) * self._win)
            echo, err = self.leak * np.abs(Y) ** 2, np.abs(E) ** 2 + 1e-10
            gain = np.clip(1 - self.suppress * echo / err, 0.1, 1.0)
            E = E * gain
        self._prev_y = y
        out = np.fft.irfft(E).astype(np.float32) * self._win
        frame = self._tail + out[:B]
        self._tail = out[B:]
        return frame

    def _estimate_delay(self) -> None:
        """GCC-PHAT between the last half second of mic and of speaker, over plausible delays."""
        # look often until it's found, then every few seconds in case the card ran dry and it moved
        self._next_check = self.n_mic + (RATE // 2 if self.delay is None else 3 * RATE)
        L = RATE // 2                                # half a second is plenty to line up speech
        with self._far_lock:
            n_far = self.n_far
            far_recent = self._get(self._far, n_far - L, L) if n_far >= L else None
        if self.n_mic < L or far_recent is None:
            return
        if float(far_recent @ far_recent) / L < 1e-6:
            return                                   # nothing has been playing: nothing to line up
        # what was written just now is heard ~0.1 s from now; allow -50 ms .. +450 ms around that
        base = self.n_mic - n_far
        lo, hi = base - RATE // 20, base + 9 * RATE // 20
        m0 = self.n_mic - L
        mic = self._get(self._mic, m0, L)
        with self._far_lock:
            far = self._get(self._far, m0 - hi, L + hi - lo)
        if far_recent.size == 0 or float(far @ far) < 1e-6:
            return
        n = 1 << int(np.ceil(np.log2(len(far) + L)))
        F = np.fft.rfft(far, n) * np.conj(np.fft.rfft(mic, n))
        cc = np.fft.irfft(F / (np.abs(F) + 1e-9), n)[: hi - lo + 1]
        k = int(np.argmax(cc))
        strength = cc[k] / (np.median(np.abs(cc)) * 20 + 1e-9)
        if strength < 1:
            return
        delay = hi - k
        if self.delay is not None and abs(delay - self.delay) <= 16:
            self._candidate = None
            return
        if self._candidate is not None and abs(delay - self._candidate) <= 16:
            self.delay_ms = (delay - base) / 16
            log.info("echo: speaker -> mic delay %d samples (%.0f ms after writing)", delay, self.delay_ms)
            self.delay, self._candidate = delay, None
            self.reset()
        else:
            self._candidate = delay              # the same answer twice in a row before trusting it
            self._next_check = self.n_mic + RATE // 4
