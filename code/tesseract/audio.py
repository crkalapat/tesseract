"""Sound card I/O.

The mic and the amp share one I2S bus (BCLK/LRCLK), so the Pi is the clock
master and both directions run at the same fixed format: 48 kHz, 2 x 32-bit
slots. The mic streams continuously; everything downstream (wake word, VAD,
STT) wants 16 kHz mono int16, so the capture thread converts once and fans
20 ms frames out to subscribers.
"""

from __future__ import annotations

import collections
import logging
import queue
import re
import threading
import time
import wave
from pathlib import Path

import numpy as np

from . import dsp

log = logging.getLogger(__name__)

HW_RATE = 48_000
RATE = 16_000            # processing rate
FRAME = 320              # 20 ms @ 16 kHz, what webrtcvad and the VAD logic want
HW_PERIOD = 960          # 20 ms @ 48 kHz
PLAY_PERIOD = 480        # 10 ms: the card takes playback in these steps, so the mixer can keep it shallow


def find_alsa_device(preferred: str = "auto") -> str:
    if preferred != "auto":
        return preferred
    try:
        cards = Path("/proc/asound/cards").read_text()
    except OSError:
        return "default"
    for line in cards.splitlines():
        m = re.match(r"\s*(\d+)\s+\[(\S+)\s*\]", line)
        if m and ("googlevoi" in m.group(2).lower() or "voicehat" in line.lower()):
            return f"hw:CARD={m.group(2)},DEV=0"
    log.warning("audio: VoiceHAT sound card not found in /proc/asound/cards; is the overlay loaded?")
    return "default"


# ── Backends ────────────────────────────────────────────────────────────────

class AlsaCapture:
    overruns = 0          # counted for `tesseract perf`
    _warned = 0.0

    def __init__(self, device: str, channel: int):
        import alsaaudio
        self.channel = channel
        kw = dict(type=alsaaudio.PCM_CAPTURE, mode=alsaaudio.PCM_NORMAL, device=device,
                  rate=HW_RATE, channels=2, format=alsaaudio.PCM_FORMAT_S32_LE,
                  periodsize=HW_PERIOD)
        try:
            self.pcm = alsaaudio.PCM(periods=16, **kw)     # 320 ms of slack if Python stalls for a moment
            # (more slack, same latency: a read returns as soon as each 20 ms is in. Losing mic audio
            # also knocks the echo cancelling out of line, so it's worth the memory)
        except TypeError:  # pyalsaaudio < 0.10
            self.pcm = alsaaudio.PCM(**kw)
        self.rate = HW_RATE

    def read(self) -> np.ndarray | None:
        n, data = self.pcm.read()
        if n <= 0:
            if n < 0:
                AlsaCapture.overruns += 1
                now = time.monotonic()
                if now - AlsaCapture._warned > 10:
                    AlsaCapture._warned = now
                    log.warning("audio: the mic fell behind and lost audio (%d times so far); see `tesseract perf`",
                                AlsaCapture.overruns)
            return None
        s = np.frombuffer(data, dtype="<i4").reshape(-1, 2)[:, self.channel]
        return s.astype(np.float32) * (1.0 / 2**31)


class AlsaPlayback:
    rate = HW_RATE
    underruns = 0         # counted for `tesseract perf`
    _warned = 0.0

    def __init__(self, device: str):
        self.device = device
        self.pcm = None

    def open(self) -> None:
        import alsaaudio
        kw = dict(type=alsaaudio.PCM_PLAYBACK, mode=alsaaudio.PCM_NORMAL, device=self.device,
                  rate=HW_RATE, channels=2, format=alsaaudio.PCM_FORMAT_S32_LE,
                  periodsize=PLAY_PERIOD)
        try:
            self.pcm = alsaaudio.PCM(periods=16, **kw)     # 160 ms buffer in 10 ms steps
        except TypeError:
            self.pcm = alsaaudio.PCM(**kw)

    def write(self, mono: np.ndarray) -> None:
        s = (np.clip(mono, -1.0, 1.0) * 2147483000).astype("<i4")
        n = self.pcm.write(np.repeat(s, 2).tobytes())
        if n is not None and n < 0:
            # ran dry: the card restarts, which clicks (and disturbs the mic on the shared I2S bus)
            AlsaPlayback.underruns += 1
            now = time.monotonic()
            if now - AlsaPlayback._warned > 10:
                AlsaPlayback._warned = now
                log.warning("audio: the speaker ran dry (%d times so far): that's a pop. Something kept "
                            "the sound thread waiting; see `tesseract perf`", AlsaPlayback.underruns)

    def close(self, drain: bool = True) -> None:
        if self.pcm is None:
            return
        try:
            if drain and hasattr(self.pcm, "drain"):
                self.pcm.drain()
            elif not drain and hasattr(self.pcm, "drop"):
                self.pcm.drop()
        except Exception:
            pass
        self.pcm.close()
        self.pcm = None


class SoundDeviceCapture:
    """Laptop mic, for --sim development."""

    def __init__(self):
        import sounddevice as sd
        self.rate = HW_RATE
        self.stream = sd.InputStream(samplerate=HW_RATE, channels=1, dtype="float32",
                                     blocksize=HW_PERIOD)
        self.stream.start()

    def read(self) -> np.ndarray | None:
        data, _ = self.stream.read(HW_PERIOD)
        return data[:, 0].copy()


class SoundDevicePlayback:
    rate = HW_RATE

    def __init__(self):
        import sounddevice as sd
        self.sd = sd
        self.stream = None

    def open(self) -> None:
        self.stream = self.sd.OutputStream(samplerate=HW_RATE, channels=1, dtype="float32")
        self.stream.start()

    def write(self, mono: np.ndarray) -> None:
        self.stream.write(np.ascontiguousarray(mono, dtype=np.float32))

    def close(self, drain: bool = True) -> None:
        if self.stream is not None:
            (self.stream.stop if drain else self.stream.abort)()
            self.stream.close()
            self.stream = None


class SilentCapture:
    """No mic: real-time paced silence, optionally preceded by a WAV file (tests)."""

    def __init__(self, wav: str | None = None):
        self.rate = HW_RATE
        self.pending = np.zeros(0, dtype=np.float32)
        self.t = None              # the clock starts at the first read, like a real mic
        if wav:
            with wave.open(wav) as w:
                x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
                x = x.reshape(-1, w.getnchannels())[:, 0].astype(np.float32) / 32768
                self.pending = dsp.StreamResampler(w.getframerate(), HW_RATE).process(x)

    def read(self) -> np.ndarray:
        if self.t is None:
            self.t = time.monotonic()
        self.t += HW_PERIOD / HW_RATE
        time.sleep(max(0.0, self.t - time.monotonic()))
        if len(self.pending):
            out, self.pending = self.pending[:HW_PERIOD], self.pending[HW_PERIOD:]
            return np.pad(out, (0, HW_PERIOD - len(out)))
        return np.zeros(HW_PERIOD, dtype=np.float32)


class NullPlayback:
    """Discards audio at real-time pace (so timing behaves like a real speaker)."""
    rate = HW_RATE

    def open(self) -> None:
        self.t = time.monotonic()

    def write(self, mono: np.ndarray) -> None:
        self.t = max(self.t, time.monotonic()) + len(mono) / HW_RATE
        time.sleep(max(0.0, self.t - time.monotonic() - 0.1))

    def close(self, drain: bool = True) -> None:
        if drain:
            time.sleep(max(0.0, self.t - time.monotonic()))


# ── AudioIO ─────────────────────────────────────────────────────────────────

class MicFrame(np.ndarray):
    """A 20 ms mic frame that knows where it is in the stream (frames since the mic started), so a
    listener running a little behind can still say exactly when something happened."""
    index = -1


class AudioIO:
    """Continuous mic capture (fan-out to subscribers) + playback factory."""

    def __init__(self, cfg, sim: bool = False, wav_input: str | None = None):
        self.cfg = cfg
        self.gain = dsp.db_to_gain(cfg.audio.mic_gain_db)
        self.muted = False
        self.level = 0.0                     # mic level 0..1 for visuals
        self._subs: list[queue.Queue] = []
        self._raw_subs: list[queue.Queue] = []   # before echo cancelling (`tesseract interrupt-test`)
        self.frames_out = 0
        self._lock = threading.Lock()
        self._ring: collections.deque[np.ndarray] = collections.deque(maxlen=100)  # 2 s
        self._dc = dsp.DCBlocker()

        self._reopen = None
        self.echo = None                     # EchoCanceller, on the real sound card only
        if sim or wav_input:
            self._capture = self._make_sim_capture(wav_input)
            self._playback_factory = self._make_sim_playback()
        else:
            device = find_alsa_device(cfg.audio.device)
            if device == "default" and cfg.audio.device == "auto" and Path("/proc/asound").exists():
                raise RuntimeError("Tesseract's sound card isn't loaded. If you just ran install.sh, reboot "
                                   "(sudo reboot); otherwise run `tesseract doctor` to see why.")
            log.info("audio: using ALSA device %s", device)
            self._reopen = lambda: AlsaCapture(device, cfg.audio.mic_channel)
            self._capture = self._reopen()
            self._playback_factory = lambda: AlsaPlayback(device)
            if cfg.audio.echo_cancel:
                from .echo import EchoCanceller
                self.echo = EchoCanceller(cfg.audio.echo_suppress)
        self._resampler = dsp.StreamResampler(self._capture.rate, RATE)
        self._far_resampler = dsp.StreamResampler(HW_RATE, RATE)
        self._seen_underruns = self._seen_overruns = 0

    @staticmethod
    def _make_sim_capture(wav):
        if wav:
            return SilentCapture(wav)
        try:
            return SoundDeviceCapture()
        except Exception as e:  # no sounddevice / no mic
            log.info("audio: no laptop mic (%s); using silence", e)
            return SilentCapture()

    @staticmethod
    def _make_sim_playback():
        try:
            import sounddevice  # noqa: F401
            return SoundDevicePlayback
        except Exception:
            return NullPlayback

    def start(self) -> None:
        threading.Thread(target=self._run, name="audio-capture", daemon=True).start()

    def subscribe(self, maxsize: int = 200) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def subscribe_raw(self, maxsize: int = 200) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._raw_subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def recent(self, ms: int) -> np.ndarray:
        """The last `ms` of 16 kHz audio (for pre-roll before the wake word)."""
        n = max(0, ms // 20)
        frames = list(self._ring)[-n:] if n else []
        return np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)

    def output(self):
        if self.echo is not None:          # a fresh playback stream: line the echo up again
            self._far_resampler = dsp.StreamResampler(HW_RATE, RATE)
            self.echo.discontinuity()
        return self._playback_factory()

    def played(self, mix: np.ndarray) -> None:
        """The mixer just wrote this (48 kHz) to the speaker: the echo canceller's reference."""
        if self.echo is not None:
            if AlsaPlayback.underruns != self._seen_underruns:     # the speaker paused: timing moved
                self._seen_underruns = AlsaPlayback.underruns
                self.echo.discontinuity()
            self.echo.far(self._far_resampler.process(mix).astype(np.float32))

    def _run(self) -> None:
        from .playback import _realtime
        _realtime()                  # like the speaker thread: lost mic audio breaks the echo cancelling
        pending = np.zeros(0, dtype=np.float32)
        while True:
            try:
                x = self._capture.read()
            except Exception as e:
                # e.g. "Input/output error": the stream stalled. Reading it again never recovers; reopen it.
                log.error("audio: capture failed (%s); reopening the mic", e)
                time.sleep(0.5)
                if self._reopen is not None:
                    try:
                        self._capture.pcm.close()
                    except Exception:
                        pass
                    if self.echo is not None:
                        self.echo.discontinuity()
                    try:
                        self._capture = self._reopen()
                    except Exception as e2:
                        log.error("audio: couldn't reopen the mic: %s", e2)
                        time.sleep(1)
                continue
            if x is None:
                if self.echo is not None and AlsaCapture.overruns != self._seen_overruns:
                    self._seen_overruns = AlsaCapture.overruns   # lost mic audio: line the echo up again
                    self.echo.discontinuity()
                continue
            x = self._resampler.process(self._dc.process(x) * self.gain)
            pending = np.concatenate((pending, x.astype(np.float32)))
            while len(pending) >= FRAME:
                frame, pending = pending[:FRAME], pending[FRAME:]
                for q in self._raw_subs:
                    try:
                        q.put_nowait(dsp.float_to_int16(frame))
                    except queue.Full:
                        pass
                if self.echo is not None:
                    try:
                        frame = self.echo.process(frame)
                    except Exception:
                        log.exception("audio: echo cancellation failed; turning it off")
                        self.echo = None
                self._publish(dsp.float_to_int16(frame))

    def _publish(self, frame: np.ndarray) -> None:
        if self.muted:
            # Privacy: while muted, audio never leaves this function.
            frame = np.zeros_like(frame)
            self.level = 0.0
        else:
            r = dsp.rms(frame) / 32768
            self.level = dsp.level_to_unit(r)
        frame = frame.view(MicFrame)
        frame.index = self.frames_out          # the echo canceller numbers its frames the same way
        self.frames_out += 1
        self._ring.append(frame)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(frame)
            except queue.Full:
                pass  # a stalled consumer must never stall the mic


def to_wav_bytes(pcm16: np.ndarray, rate: int = RATE) -> bytes:
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16.astype("<i2").tobytes())
    return buf.getvalue()
