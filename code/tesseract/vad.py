"""End-of-speech detection: records one utterance after the wake word.

Endpointing is one of the biggest latency levers in a voice assistant: every
millisecond of trailing silence we wait for is a millisecond before the
request even leaves the device. A fixed hang time has to be long, or it cuts
people off mid-pause. Instead this transcribes speculatively:

    you stop ─ 200 ms ─► send what we have to speech-to-text, keep listening
                         ├ transcript is a finished sentence ─► done at 750 ms, transcript already in hand
                         ├ it trails off ("set a timer for") ─► wait up to end_silence_ms (1.2 s)
                         └ you start talking again ──────────► throw the guess away

so a complete request is answered about one STT round trip sooner, and a
hesitant one is less likely to be cut off. Speech detection itself is
Silero VAD (a 2 MB neural model), which doesn't mistake a fan or hum for a voice.
"""

from __future__ import annotations

import collections
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import dsp
from .audio import FRAME, RATE

log = logging.getLogger(__name__)

FRAME_MS = FRAME * 1000 // RATE  # 20
GUESS_GRACE_S = 1.0              # after you stop, how long to wait on an early transcription before racing a new one


SILERO_URL = "https://github.com/snakers4/silero-vad/raw/v6.2.3/src/silero_vad/data/silero_vad.onnx"
SILERO_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
SILERO_FILE = "silero_vad.onnx"


def ensure_silero(models_dir: Path) -> Path:
    """Fetch Silero VAD (2.3 MB, pinned to a release and checked) if it isn't there yet."""
    dst = models_dir / SILERO_FILE
    if dst.exists():
        return dst
    import hashlib

    import httpx
    log.info("vad: downloading Silero VAD")
    r = httpx.get(SILERO_URL, follow_redirects=True, timeout=60)
    r.raise_for_status()
    if hashlib.sha256(r.content).hexdigest() != SILERO_SHA256:
        raise RuntimeError("Silero VAD download didn't match its checksum")
    models_dir.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part")
    tmp.write_bytes(r.content)
    tmp.replace(dst)
    return dst


class SileroDetector:
    """Is this 20 ms frame speech? Silero VAD, a small neural model that tells voices from fans, hum
    and hiss, which webrtcvad + a loudness gate called "speech" 86-100% of the time (so listening
    never ended, and Whisper filled the extra seconds with "Thank you. Thank you.").

    It works on 32 ms chunks; the answer for each 20 ms frame is the average of the last three
    chunks, so one noisy blip can't restart the end-of-speech countdown."""

    CHUNK, CONTEXT = 512, 64

    _sessions: dict = {}          # one loaded model for every utterance (loading takes ~0.1 s on the Pi)

    def __init__(self, model: Path, threshold: float = 0.5):
        if model not in self._sessions:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = opts.inter_op_num_threads = 1
            opts.log_severity_level = 3
            self._sessions[model] = ort.InferenceSession(str(model), sess_options=opts,
                                                         providers=["CPUExecutionProvider"])
        self.session = self._sessions[model]
        self.threshold = threshold
        self._state = np.zeros((2, 1, 128), np.float32)
        self._ctx = np.zeros((1, self.CONTEXT), np.float32)
        self._buf = np.zeros(0, np.float32)
        self._recent = collections.deque([0.0] * 3, maxlen=3)
        self._sr = np.array(RATE, np.int64)

    def prime(self, frames: np.ndarray) -> None:
        pass                                     # it needs no noise floor

    def __call__(self, frame: np.ndarray) -> bool:
        self._buf = np.concatenate((self._buf, frame.astype(np.float32) / 32768))
        while len(self._buf) >= self.CHUNK:
            x = np.concatenate((self._ctx, self._buf[None, :self.CHUNK]), axis=1)
            self._buf = self._buf[self.CHUNK:]
            p, self._state = self.session.run(None, {"input": x, "state": self._state, "sr": self._sr})
            self._ctx = x[:, -self.CONTEXT:]
            self._recent.append(float(p[0, 0]))
        return sum(self._recent) / len(self._recent) > self.threshold


def speech_detector(cfg):
    """Silero when its model is there (app.py fetches it at start-up), else webrtcvad."""
    model = cfg.path("models") / SILERO_FILE
    if not model.exists():
        log.warning("vad: %s isn't downloaded yet; using webrtcvad", model)
        return SpeechDetector(cfg.listen.vad_aggressiveness)
    try:
        return SileroDetector(model, cfg.listen.vad_threshold)
    except Exception as e:
        log.warning("vad: Silero unavailable (%s); using webrtcvad", e)
        return SpeechDetector(cfg.listen.vad_aggressiveness)


class SpeechDetector:
    def __init__(self, aggressiveness: int = 2):
        try:
            import webrtcvad
            self.vad = webrtcvad.Vad(aggressiveness)
        except ImportError:
            log.warning("vad: webrtcvad not installed; using energy-only detection")
            self.vad = None
        self.noise = None

    def prime(self, frames: np.ndarray) -> None:
        """Seed the noise floor from audio heard before the user spoke."""
        if len(frames):
            self.noise = max(30.0, dsp.rms(frames) * 0.8)

    def __call__(self, frame: np.ndarray) -> bool:
        level = dsp.rms(frame)
        if self.noise is None:
            self.noise = max(30.0, level)
        loud = level > max(120.0, self.noise * 2.0)
        voiced = self.vad.is_speech(frame.tobytes(), RATE) if self.vad else loud
        speech = loud and voiced
        if not speech:  # track the floor only on non-speech (fast down, slow up)
            k = 0.2 if level < self.noise else 0.02
            self.noise += k * (level - self.noise)
        return speech


# a transcript ending in one of these is probably mid-sentence ("set a timer for.")
_DANGLING = set("and or but so because to for of the a an with at in on from about my your is are was what "
                "whats um uh like then if that this into by as than".split())


def looks_complete(text: str) -> bool:
    t = text.strip()
    if not t or t[-1] not in ".?!" or t.endswith(("...", "…")):
        return False
    words = re.findall(r"[a-z']+", t.lower())
    return bool(words) and words[-1].replace("'", "") not in _DANGLING


@dataclass
class Utterance:
    audio: np.ndarray          # 16 kHz int16
    text: str | None = None    # transcript, if the speculative one covers all of the audio
    ended_at: float = 0.0      # time.monotonic() when you stopped talking


class _Guess:
    """A speech-to-text request for the audio so far, running in the background."""

    def __init__(self, transcribe, pcm: np.ndarray):
        self.done = threading.Event()
        self.text: str | None = None
        threading.Thread(target=self._run, args=(transcribe, pcm), name="stt-early", daemon=True).start()

    def _run(self, transcribe, pcm) -> None:
        try:
            self.text = transcribe(pcm)
        except Exception as e:
            log.debug("vad: early transcription failed: %s", e)
        finally:
            self.done.set()


def record_utterance(cfg, audio, cancel: threading.Event, preroll: np.ndarray | None = None,
                     ignore_until: float = 0.0, start_timeout: float | None = None,
                     on_speech_start=None, transcribe=None) -> Utterance | None:
    """Blocks until the user stops talking. Returns the 16 kHz int16 audio (and,
    given `transcribe`, usually its transcript), or None if nobody spoke before
    the timeout (or we were cancelled)."""
    L = cfg.listen
    q = audio.subscribe()
    guess: _Guess | None = None
    try:
        det = speech_detector(cfg)
        chunks = [preroll] if preroll is not None and len(preroll) else []
        if chunks:
            det.prime(preroll[: len(preroll) // 2])
        started = False
        run = 0                  # consecutive speech frames
        silence_ms = 0
        speech_ms = 0
        t0 = time.monotonic()
        timeout = start_timeout if start_timeout is not None else L.start_timeout_s

        while not cancel.is_set():
            try:
                frame = q.get(timeout=0.5)
            except queue.Empty:
                continue
            now = time.monotonic()
            chunks.append(frame)
            if now < ignore_until:   # our own chime is playing
                continue
            is_speech = det(frame)

            if not started:
                run = run + 1 if is_speech else 0
                if run >= 3:     # 60 ms of speech
                    started = True
                    speech_ms = run * FRAME_MS
                    if on_speech_start:
                        on_speech_start()
                elif now - t0 > timeout:
                    return None
                else:
                    # keep only a little lead-in while waiting
                    keep = max(1, cfg.listen.preroll_ms // FRAME_MS) + 10
                    if len(chunks) > keep + 50:
                        chunks = chunks[-keep:]
                continue

            if is_speech:
                silence_ms = 0
                speech_ms += FRAME_MS
                guess = None             # they kept talking; that transcript is stale
            else:
                silence_ms += FRAME_MS
            if transcribe is not None and guess is None and silence_ms >= L.early_silence_ms:
                guess = _Guess(transcribe, np.concatenate(chunks))
            # a long explanation has thinking pauses in it: be more patient once it's gone on a while
            long_talk = speech_ms >= L.long_talk_after_s * 1000
            complete_ms = max(L.complete_silence_ms, L.long_pause_ms) if long_talk else L.complete_silence_ms
            end_ms = max(L.end_silence_ms, L.long_pause_ms) if long_talk else L.end_silence_ms
            if guess is not None and silence_ms >= complete_ms and guess.done.is_set() \
                    and guess.text is not None and looks_complete(guess.text):
                break                    # a finished sentence, and a real pause after it
            if silence_ms >= end_ms:
                break
            if now - t0 > L.max_utterance_s:
                log.info("vad: max utterance length reached")
                break

        if cancel.is_set() or not started:
            return None
        ended_at = time.monotonic() - silence_ms / 1000
        # drop most of the trailing silence; STT doesn't need it
        trim = max(0, (silence_ms - 200) // FRAME_MS)
        if trim:
            chunks = chunks[:-trim]
        text = None
        if guess is not None:            # nothing was said after it was taken, so it covers everything
            if guess.done.wait(GUESS_GRACE_S):
                text = guess.text
            else:
                # slow or stuck: race a fresh transcription against it and take whichever answers first
                log.info("vad: early transcription is slow; racing a fresh one")
                fresh = _Guess(transcribe, np.concatenate(chunks))
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if guess.done.is_set() and guess.text is not None:
                        text = guess.text
                        break
                    if fresh.done.is_set():
                        text = fresh.text
                        break
                    time.sleep(0.02)
        return Utterance(np.concatenate(chunks), text, ended_at)
    finally:
        audio.unsubscribe(q)
