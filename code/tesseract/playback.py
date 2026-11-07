"""Single owner of the speaker.

An ALSA hw device can only be opened once, and speech, chimes and alarms can
overlap, so everything that makes sound hands a *source* to the Mixer. The
mixer thread pulls 20 ms from every active source, sums, applies the volume
and writes to the card. A source that has nothing ready yet (TTS still
downloading) just contributes silence, so the card never underruns or clicks.
"""

from __future__ import annotations

import collections
import logging
import threading
import time

import numpy as np

from . import dsp
from .audio import HW_RATE

log = logging.getLogger(__name__)

BLOCK = 960            # 20 ms @ 48 kHz
# While a source is waiting for audio (TTS still downloading), keep only this much
# silence queued in the sound card. Writing silence as fast as the card takes it
# fills its whole buffer (~128 ms), and the first word of the reply then waits
# behind all of it. But it must stay several of the card's 10 ms playback steps ahead:
# at 30 ms a Pi Zero kept running dry, and every restart was a pop; 60 ms still slipped
# now and then during bursts of network work.
IDLE_LEAD = 0.08


class Source:
    gain = 1.0
    min_volume = 0.0      # floor under the user's volume (alarms must be audible at volume 0)
    finished = False
    background = False    # music: always there, not "busy", and stop_all() leaves it playing

    def read(self, n: int) -> np.ndarray | None:
        """Return up to n samples (float32 mono 48 kHz), or None when finished."""
        raise NotImplementedError

    def ready(self) -> bool:
        """Has audio to play right now (or is about to finish)."""
        return True

    def stop(self) -> None:
        self.finished = True


class ArraySource(Source):
    def __init__(self, samples: np.ndarray, gain: float = 1.0, loop: bool = False):
        self.samples, self.gain, self.loop, self.pos = samples, gain, loop, 0
        self.finished = False

    def read(self, n):
        if self.finished:
            return None
        if self.pos >= len(self.samples):
            if not self.loop:
                self.finished = True
                return None
            self.pos = 0
        out = self.samples[self.pos:self.pos + n]
        self.pos += len(out)
        return out


class StreamSource(Source):
    """Fed incrementally by a producer thread (streaming TTS).

    The voice arrives over the network in pieces. If the next piece is late, cutting from mid-waveform
    straight to silence (and back) is a pop each way: the crackle you heard on a slow connection. So it
    keeps a little in hand before starting (PREBUFFER), and when it does run dry it fades out over a
    few milliseconds, waits for a bigger cushion (REBUFFER), and fades back in."""

    PREBUFFER = int(0.15 * HW_RATE)
    REBUFFER = int(0.3 * HW_RATE)
    FADE = int(0.004 * HW_RATE)

    def __init__(self, on_audio=None):
        self._chunks: collections.deque[np.ndarray] = collections.deque()
        self._n = 0
        self._lock = threading.Lock()
        self._closed = False
        self.finished = False
        self.on_audio = on_audio          # called whenever sound (re)starts flowing
        self._flowing = False
        self._need = self.PREBUFFER       # samples to have in hand before (re)starting
        self._fade_in = False
        self._dry_since = 0.0
        self.dry_count = 0
        self.done = threading.Event()
        # captions: on_mark(text) fires when playback reaches the sample where mark(text) was placed
        self.on_mark = None
        self._marks: collections.deque[tuple[int, str]] = collections.deque()
        self._pushed = 0
        self._played = 0

    def mark(self, text: str) -> None:
        """What's said from here on (the next sentence), for the screen's captions."""
        with self._lock:
            self._marks.append((self._pushed, text))

    def push(self, x: np.ndarray) -> None:
        if len(x):
            with self._lock:
                self._chunks.append(x)
                self._n += len(x)
                self._pushed += len(x)

    def close(self) -> None:
        self._closed = True

    def ready(self) -> bool:
        return self._n >= self._need or self._closed or self.finished

    def stop(self) -> None:
        self.finished = True
        self.done.set()

    def read(self, n):
        if self.finished:
            return None
        if self._n < self._need and not self._closed:
            return np.zeros(0, dtype=np.float32)          # still gathering a cushion
        out, need = [], n
        with self._lock:
            while need and self._chunks:
                c = self._chunks[0]
                if len(c) <= need:
                    out.append(self._chunks.popleft())
                    need -= len(c)
                else:
                    out.append(c[:need])
                    self._chunks[0] = c[need:]
                    need = 0
            self._n -= n - need
            self._played += n - need
            reached = []
            while self._marks and self._marks[0][0] < self._played:
                reached.append(self._marks.popleft()[1])
            empty = not self._chunks
        if reached and self.on_mark:
            try:
                self.on_mark(reached[-1])
            except Exception:
                log.exception("captions: on_mark failed")
        if not out:
            if empty and self._closed:
                self.finished = True
                self.done.set()
                return None
            return np.zeros(0, dtype=np.float32)
        x = np.concatenate(out)
        self._need = 0
        if self._fade_in:                                  # coming back after running dry
            k = min(self.FADE, len(x))
            x = x.copy()
            x[:k] *= np.linspace(0, 1, k, dtype=np.float32)
            self._fade_in = False
            log.info("tts: the voice stream ran dry mid-word for %.0f ms (slow connection?); smoothed over",
                     (time.monotonic() - self._dry_since) * 1000)
        if empty and not self._closed:
            tail = x[-min(len(x), 480):]
            if float(np.sqrt(np.mean(tail * tail))) > 0.02:   # mid-word, not the pause after a sentence
                k = min(self.FADE, len(x))
                x = x.copy()
                x[len(x) - k:] *= np.linspace(1, 0, k, dtype=np.float32)
                self._need, self._fade_in, self._dry_since = self.REBUFFER, True, time.monotonic()
                self.dry_count += 1
            else:                                          # between sentences: a small cushion for the next
                self._need = self.PREBUFFER
        if not self._flowing and self.on_audio:
            self.on_audio()
        self._flowing = True
        return x


def soft_limit(x: np.ndarray, knee: float = 0.8) -> np.ndarray:
    """Round off peaks above `knee` instead of clipping them flat at full scale: hard clipping is a
    crackle on every loud syllable at high volume. Untouched below the knee."""
    peak = float(np.abs(x).max()) if len(x) else 0.0
    if peak <= knee:
        return x
    a = np.abs(x)
    over = a > knee
    y = x.copy()
    y[over] = np.sign(x[over]) * (knee + (1 - knee) * np.tanh((a[over] - knee) / (1 - knee)))
    return y


def _realtime() -> None:
    """Ask Linux to run the speaker thread ahead of everything else (needs root, as the service is),
    so a busy moment elsewhere doesn't let the sound card run dry."""
    import os
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(20))
    except (AttributeError, OSError):
        pass


class Mixer:
    def __init__(self, audio, volume_gain):
        self.audio = audio
        self.volume_gain = volume_gain          # callable -> linear gain
        self.sources: list[Source] = []
        self._cv = threading.Condition()
        self._levels: collections.deque[tuple[float, float]] = collections.deque(maxlen=64)
        self._out = None
        self._open_now = False
        self._closing = False
        self._closed = threading.Event()

    def start(self, open_now: bool = True) -> None:
        """open_now: start the sound card right away. Once open it stays open, fed with
        silence between sounds: stopping and restarting the I2S clocks is what makes the
        amp click, and a card that runs dry replays stale buffer contents ("zit zit")."""
        self._open_now = open_now
        threading.Thread(target=self._run, name="mixer", daemon=True).start()

    def close(self, timeout: float = 5.0) -> None:
        """Let what's playing finish, end on a little silence, and stop the card cleanly."""
        with self._cv:
            self._closing = True
            self._cv.notify()
        self._closed.wait(timeout)

    def play(self, source: Source) -> Source:
        with self._cv:
            self.sources.append(source)
            self._cv.notify()
        return source

    def stop_all(self) -> None:
        with self._cv:
            for s in self.sources:
                if not s.background:
                    s.stop()

    @property
    def busy(self) -> bool:
        """Something other than music is playing (a reply, a chime, an alarm)."""
        return any(not s.finished and not s.background for s in self.sources)

    def level(self) -> float:
        """Loudness (0..1) of what is coming out of the speaker *right now*."""
        now = time.monotonic()
        lvl = 0.0
        for t, v in self._levels:
            if t > now:
                break
            lvl = v
        return lvl

    def _run(self) -> None:
        _realtime()
        played_until = 0.0
        while True:
            with self._cv:
                self.sources = [s for s in self.sources if not s.finished]
                sources = list(self.sources)
                if not sources and self._closing:
                    break
                if not sources and self._out is None and not self._open_now:
                    self._cv.wait(timeout=0.25)
                    continue
            # nothing ready (or nothing playing at all): keep only IDLE_LEAD of silence queued
            if self._out is not None and played_until - time.monotonic() > IDLE_LEAD and \
                    not any(s.ready() for s in sources):
                time.sleep(0.005)    # nothing to play yet and the card has enough queued
                continue
            mix = np.zeros(BLOCK, dtype=np.float32)
            vol = self.volume_gain()
            for s in sources:
                try:
                    x = s.read(BLOCK)
                except Exception:
                    log.exception("mixer: source failed")
                    s.stop()
                    continue
                if x is not None and len(x):
                    mix[: len(x)] += x * (s.gain * max(vol, s.min_volume))
            mix = soft_limit(mix)
            try:
                if self._out is None:
                    self._out = self.audio.output()
                    self._out.open()
                    played_until = time.monotonic()
                self._out.write(mix)
                if hasattr(self.audio, "played"):
                    self.audio.played(mix)     # the echo canceller's reference
            except Exception:
                log.exception("mixer: playback failed")
                self._close()
                time.sleep(0.5)
                continue
            # When will this block be heard? Used to sync LEDs to the voice.
            played_until = max(played_until, time.monotonic()) + BLOCK / HW_RATE
            self._levels.append((played_until, dsp.level_to_unit(dsp.rms(mix), -45, -8)))
        # closing: end on 100 ms of silence so the card never stops mid-sound, then drain
        if self._out is not None:
            try:
                for _ in range(5):
                    self._out.write(np.zeros(BLOCK, dtype=np.float32))
            except Exception:
                pass
        self._close()
        self._closed.set()

    def _close(self) -> None:
        if self._out is not None:
            try:
                self._out.close(drain=True)
            except Exception:
                pass
            self._out = None
