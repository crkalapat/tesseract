"""Wake word detection ("Franklin").

Engines:

  both          (default) the trained "Franklin" model (models/franklin.onnx, openWakeWord
                style, trained on 6,000 clips from 800 synthetic speakers) and sherpa: either
                one wakes it. They miss different things, so together they catch more.
  sherpa        sherpa-onnx open-vocabulary keyword spotting. It
                listens for whatever word you type, so no training: a small
                streaming speech model turns audio into word pieces and
                fires when the pieces for "FRANKLIN" come out.
  openwakeword  a model trained for one phrase (more robust, but training
                means the openWakeWord notebook).
  porcupine     Picovoice, needs an access key.

openWakeWord's Python package drags in scikit-learn, scipy and a TFLite
runtime at import time, which is a lot for a 512 MB Pi. Its inference is
just three small ONNX models chained together, so this file runs them
directly with onnxruntime. tests/test_wakeword_parity.py checks the scores
match the official package.

    16 kHz audio -> melspectrogram.onnx -> 76x32 mel window
                 -> embedding_model.onnx -> 96-d embedding every 80 ms
                 -> <wakeword>.onnx over the last 16 embeddings -> score 0..1
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

OWW_RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
FEATURE_MODELS = ("melspectrogram.onnx", "embedding_model.onnx")
PRETRAINED = {"hey_jarvis": "hey_jarvis_v0.1.onnx", "alexa": "alexa_v0.1.onnx",
              "hey_mycroft": "hey_mycroft_v0.1.onnx", "hey_rhasspy": "hey_rhasspy_v0.1.onnx"}

CHUNK = 1280  # 80 ms

# English streaming zipformer (GigaSpeech), int8: ~5 MB on disk
KWS_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
KWS_MODEL = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
KWS_NET = "epoch-12-avg-2-chunk-16-left-64.int8.onnx"
KWS_FILES = ("tokens.txt", "bpe.model", f"encoder-{KWS_NET}", f"decoder-{KWS_NET}", f"joiner-{KWS_NET}")


def ensure_model_files(models_dir: Path, names: list[str]) -> None:
    import httpx
    models_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        dst = models_dir / name
        if dst.exists() and dst.stat().st_size > 0:
            continue
        log.info("wakeword: downloading %s", name)
        r = httpx.get(OWW_RELEASE + name, follow_redirects=True, timeout=60)
        r.raise_for_status()
        tmp = dst.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(dst)


def ensure_kws_model(models_dir: Path) -> Path:
    """Fetch the keyword-spotting model (one 17 MB archive) and keep only the files we use."""
    import io
    import tarfile

    import httpx
    d = models_dir / KWS_MODEL
    if all((d / f).exists() for f in KWS_FILES):
        return d
    log.info("wakeword: downloading %s", KWS_MODEL)
    r = httpx.get(KWS_RELEASE + KWS_MODEL + ".tar.bz2", follow_redirects=True, timeout=120)
    r.raise_for_status()
    d.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:bz2") as tar:
        for name in KWS_FILES:   # by exact name, so the archive can't write anywhere else
            data = tar.extractfile(f"{KWS_MODEL}/{name}").read()
            tmp = d / (name + ".part")
            tmp.write_bytes(data)
            tmp.replace(d / name)
    return d


class OpenWakeWordDetector:
    def __init__(self, model_path: Path, models_dir: Path, frames: int = 1):
        """frames: the score must stay up for this many 80 ms steps in a row. A real "Franklin" holds
        its peak; a random syllable that resembles it spikes for one step (2 frames: a third of the
        false wakes in 3 hours of real-world audio, for a few % fewer catches)."""
        import collections

        import onnxruntime as ort
        self.frames = max(1, int(frames))
        self._recent: collections.deque = collections.deque(maxlen=self.frames)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1   # one core is plenty; leave the rest for audio/UI
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3

        def session(p: Path):
            return ort.InferenceSession(str(p), sess_options=opts, providers=["CPUExecutionProvider"])

        self.mel = session(models_dir / "melspectrogram.onnx")
        self.emb = session(models_dir / "embedding_model.onnx")
        self.ww = session(model_path)
        self.ww_input = self.ww.get_inputs()[0].name
        self.n_feats = int(self.ww.get_inputs()[0].shape[1])
        self.name = model_path.stem
        self.reset()

    def reset(self) -> None:
        self.raw = np.zeros(480, dtype=np.float32)            # 3 frames of mel context
        self.mel_buf = np.ones((76, 32), dtype=np.float32)
        self.feats = np.zeros((self.n_feats, 96), dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.int16)
        self.warmup = self.n_feats + 4                       # chunks before scores are trusted
        self._recent.clear()

    def process(self, pcm16: np.ndarray) -> float:
        """Feed any number of samples; returns the max score of completed chunks."""
        self.pending = np.concatenate((self.pending, pcm16))
        best = 0.0
        while len(self.pending) >= CHUNK:
            chunk, self.pending = self.pending[:CHUNK], self.pending[CHUNK:]
            best = max(best, self._chunk(chunk))
        return best

    def _chunk(self, chunk: np.ndarray) -> float:
        self.raw = np.concatenate((self.raw, chunk.astype(np.float32)))[-(CHUNK + 480):]
        mel = self.mel.run(None, {"input": self.raw[None, :]})[0]
        mel = np.squeeze(mel) / 10 + 2
        self.mel_buf = np.vstack((self.mel_buf, mel))[-76:]
        emb = self.emb.run(None, {"input_1": self.mel_buf[None, :, :, None]})[0].reshape(-1)
        self.feats = np.vstack((self.feats[1:], emb))
        score = float(self.ww.run(None, {self.ww_input: self.feats[None]})[0].reshape(-1)[0])
        if self.warmup > 0:
            self.warmup -= 1
            return 0.0
        self._recent.append(score)
        return min(self._recent) if len(self._recent) == self.frames else 0.0


def keyword_lines(bpe, keyword: str, variants, threshold: float, boost: float) -> list[str]:
    """sherpa's keywords file: one line per spelling, all labelled with the keyword. A variant can
    carry its own threshold ("Frankin#0.45") when it also matches everyday words."""
    label = keyword.upper().replace(" ", "_")
    lines = []
    for spelling in [keyword, *variants]:
        word, _, thr = spelling.partition("#")
        tokens = " ".join(bpe.encode(word.strip().upper(), out_type=str))   # "FRANKLIN" -> "▁F RA N K LI N"
        lines.append(f"{tokens} :{boost} #{float(thr) if thr else threshold} @{label}")
    return lines


class SherpaKeywordDetector:
    def __init__(self, keyword: str, models_dir: Path, threshold: float, boost: float, variants=()):
        import sentencepiece
        import sherpa_onnx

        d = ensure_kws_model(models_dir)
        bpe = sentencepiece.SentencePieceProcessor(model_file=str(d / "bpe.model"))
        lines = keyword_lines(bpe, keyword, variants, threshold, boost)
        self.tokens = lines[0].split(" :")[0]
        kw_file = d / "keywords.txt"
        kw_file.write_text("\n".join(lines) + "\n")
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(d / "tokens.txt"), encoder=str(d / f"encoder-{KWS_NET}"),
            decoder=str(d / f"decoder-{KWS_NET}"), joiner=str(d / f"joiner-{KWS_NET}"),
            keywords_file=str(kw_file), keywords_threshold=threshold, keywords_score=boost,
            num_threads=1, max_active_paths=4, num_trailing_blanks=1)
        self.name = f"{keyword} (sherpa)"
        self.reset()

    def reset(self) -> None:
        self.stream = self.kws.create_stream()

    def process(self, pcm16: np.ndarray) -> float:
        self.stream.accept_waveform(16000, pcm16.astype(np.float32) / 32768)
        hit = 0.0
        while self.kws.is_ready(self.stream):
            self.kws.decode_stream(self.stream)
            if self.kws.get_result(self.stream):
                hit = 1.0
                self.kws.reset_stream(self.stream)
        return hit


class PorcupineDetector:
    def __init__(self, keyword_path: Path, sensitivity: float):
        import pvporcupine
        key = os.environ.get("PORCUPINE_ACCESS_KEY", "")
        if not key:
            raise RuntimeError("PORCUPINE_ACCESS_KEY missing from .env")
        self.p = pvporcupine.create(access_key=key, keyword_paths=[str(keyword_path)],
                                    sensitivities=[sensitivity])
        self.name = keyword_path.stem
        self.reset()

    def reset(self) -> None:
        self.pending = np.zeros(0, dtype=np.int16)

    def process(self, pcm16: np.ndarray) -> float:
        self.pending = np.concatenate((self.pending, pcm16))
        n = self.p.frame_length
        hit = 0.0
        while len(self.pending) >= n:
            frame, self.pending = self.pending[:n], self.pending[n:]
            if self.p.process(frame.tolist()) >= 0:
                hit = 1.0
        return hit


class EitherDetector:
    """Two detectors on the same audio; the higher score wins. The trained "Franklin" model
    (openWakeWord, models/franklin.onnx) and sherpa's keyword spotter miss different things."""

    def __init__(self, *detectors):
        self.detectors = detectors
        self.name = " + ".join(d.name for d in detectors)

    def reset(self) -> None:
        for d in self.detectors:
            d.reset()

    def process(self, pcm16: np.ndarray) -> float:
        return max(d.process(pcm16) for d in self.detectors)


def make_detector(cfg):
    w = cfg.wake
    models_dir = cfg.path("models")
    if w.engine == "both":
        found = []
        model = cfg.path(w.model)
        if model.exists():
            ensure_model_files(models_dir, list(FEATURE_MODELS))
            found.append(OpenWakeWordDetector(model, models_dir, w.frames))
        try:
            found.append(SherpaKeywordDetector(w.keyword or cfg.assistant.name, models_dir, w.keyword_threshold,
                                               w.keyword_boost, w.keyword_variants))
        except Exception:
            log.exception("wakeword: sherpa keyword spotter failed")
        if found:
            return found[0] if len(found) == 1 else EitherDetector(*found)
        log.error("wakeword: neither %s nor sherpa is available", model)
    if w.engine == "porcupine":
        return PorcupineDetector(cfg.path(w.porcupine_keyword), w.porcupine_sensitivity)
    if w.engine == "sherpa":
        try:
            keyword = w.keyword or cfg.assistant.name
            variants = w.keyword_variants if keyword.lower() == "franklin" or w.keyword else []
            return SherpaKeywordDetector(keyword, models_dir, w.keyword_threshold, w.keyword_boost, variants)
        except Exception:
            # a headless speaker that can't start is worse than one using the fallback word
            log.exception("wakeword: sherpa keyword spotter failed; falling back to openWakeWord")

    model = cfg.path(w.model)
    if not model.exists():
        fallback = PRETRAINED.get(w.fallback_model, w.fallback_model)
        log.warning("wakeword: %s not found. Using fallback '%s' until you train an Franklin model "
                    "(see README)", model, w.fallback_model)
        ensure_model_files(models_dir, [fallback])
        model = models_dir / fallback
    ensure_model_files(models_dir, list(FEATURE_MODELS))
    return OpenWakeWordDetector(model, models_dir, w.frames)


class WakeWordListener:
    """Runs the detector(s) on the live mic stream, each in its own thread.

    It must keep up with the mic. If it falls behind, "Franklin" is heard seconds late, in silence,
    and two said in a row both fire out of the backlog. So: the two detectors of the "both" engine run
    in parallel (onnxruntime and sherpa release the GIL, so they get a core each); if one is ever more
    than a second behind the mic it skips ahead rather than catch up; a detection of audio that's too
    old is dropped; and "don't fire twice" is 1.5 s of *audio*, not of wall-clock time."""

    MAX_BEHIND = 50      # frames (1 s): further behind than this, skip ahead to the present
    STALE = 75           # frames (1.5 s): a detection this old is no use to anyone
    REFRACTORY = 75      # frames (1.5 s of audio) after a wake before another counts

    def __init__(self, cfg, audio, on_wake: Callable[[float], None],
                 threshold_boost: Callable[[], float] = lambda: 0.0):
        self.cfg = cfg
        self.audio = audio
        self.on_wake = on_wake
        self.threshold_boost = threshold_boost
        self.detector = make_detector(cfg)
        self._parts = self.detector.detectors if isinstance(self.detector, EitherDetector) else (self.detector,)
        self.paused = False
        self.last_score = 0.0
        self.wake_position: int | None = None   # mic frame number the last wake fired on (audio.MicFrame)
        self.behind_s = 0.0                      # how far behind the mic the wake word is running, lately
        self.skips = 0
        self._lock = threading.Lock()
        self._last_frame = -10 ** 9
        self._refractory_until = 0.0
        self._warned = 0.0
        self.running = True
        self._threads: list[threading.Thread] = []

    @property
    def model_name(self) -> str:
        return self.detector.name

    def start(self) -> None:
        for i, det in enumerate(self._parts):
            q = self.audio.subscribe()   # before the thread runs, so no early audio is missed
            th = threading.Thread(target=self._run, args=(det, q), name=f"wakeword-{i}", daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        """Stop before interpreter exit: onnxruntime aborts if torn down mid-inference."""
        self.running = False
        for th in self._threads:
            th.join(timeout=1)

    def _behind(self, frame) -> int:
        pos = getattr(frame, "index", None)
        latest = getattr(self.audio, "frames_out", None)
        return 0 if pos is None or latest is None else max(0, latest - 1 - pos)

    def _run(self, det, q) -> None:
        was_paused = False
        while self.running:
            try:
                frame = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self.paused or self.audio.muted:
                was_paused = True
                continue
            if was_paused:
                # Stale context from before the pause would skew the first scores.
                det.reset()
                was_paused = False
            frames = [frame]
            while True:
                try:
                    frames.append(q.get_nowait())
                except queue.Empty:
                    break
            oldest = self._behind(frames[0])            # how stale the audio about to be processed is
            self.behind_s += 0.1 * (oldest / 50 - self.behind_s)
            if oldest > self.MAX_BEHIND:
                # too far behind to be useful: drop the backlog and listen from (nearly) now
                frames = frames[-10:]
                det.reset()
                self.skips += 1
                now = time.monotonic()
                if now - self._warned > 30:
                    self._warned = now
                    log.warning("wakeword: %s fell %.1f s behind the mic; skipped ahead (the Pi is too busy: "
                                "see `tesseract perf`)", det.name, oldest / 50)
            behind = self._behind(frames[-1])
            score = det.process(np.concatenate(frames))
            self.last_score = score
            threshold = min(0.98, self.cfg.wake.threshold + self.threshold_boost())
            if score >= threshold:
                self._fire(det, score, getattr(frames[-1], "index", None), behind)

    def _fire(self, det, score: float, pos: int | None, behind: int) -> None:
        with self._lock:
            now = time.monotonic()
            if pos is not None:
                if pos - self._last_frame < self.REFRACTORY or behind > self.STALE:
                    return
                self._last_frame = pos
            elif now < self._refractory_until:
                return
            self._refractory_until = now + 1.5
            self.wake_position = pos
        log.info("wakeword: detected by %s (score %.2f, %.2f s behind the mic)", det.name, score, behind / 50)
        det.reset()
        try:
            self.on_wake(score)
        except Exception:
            log.exception("wakeword: on_wake handler failed")
