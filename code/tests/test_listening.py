"""Listening ends when you stop, even with a fan on; Whisper's invented "Thank you"s are removed."""

import queue
import threading
from pathlib import Path

import numpy as np
import pytest

from tesseract import config, vad
from tesseract.cloud import clean_transcript

MODEL = Path(__file__).resolve().parent.parent / "models" / vad.SILERO_FILE


@pytest.mark.parametrize("heard,meant", [
    ("What's the weather tomorrow? Thank you. Thank you. Thank you.", "What's the weather tomorrow?"),
    ("Turn it up thank you thank you thank you", "Turn it up"),
    ("Play Blinding Lights. Thanks for watching!", "Play Blinding Lights."),
    ("Thank you. Thank you.", ""),
    ("Set a timer for ten minutes. Thank you.", "Set a timer for ten minutes. Thank you."),   # you might mean it
    ("Thank you.", "Thank you."),
    ("Thank you for the help, what time is it?", "Thank you for the help, what time is it?"),
])
def test_whisper_ghosts(heard, meant):
    assert clean_transcript(heard) == meant


def _voice(rng, seconds):
    """Something speech-like: a pitched, syllable-modulated buzz with formant-ish filtering."""
    t = np.arange(int(16000 * seconds)) / 16000
    f0 = 140 + 30 * np.sin(2 * np.pi * 0.7 * t)
    x = np.sign(np.sin(2 * np.pi * np.cumsum(f0) / 16000)) * 0.5
    x = np.convolve(x, np.hanning(24), "same")
    syll = (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t)) ** 2
    return (x * syll * 0.3 + rng.normal(0, 0.002, len(t))).astype(np.float32)


class _Feed(queue.Queue):
    def __init__(self, x, cancel):
        super().__init__()
        self.cancel = cancel
        pcm = np.clip(x * 32768, -32768, 32767).astype(np.int16)
        for i in range(0, len(pcm) - 319, 320):
            self.put(pcm[i:i + 320])

    def get(self, block=True, timeout=None):
        if self.empty():
            self.cancel.set()                     # it was still listening when the audio ran out
            raise queue.Empty
        return queue.Queue.get(self, False)


@pytest.mark.skipif(not MODEL.exists(), reason="models/silero_vad.onnx not downloaded")
def test_listening_ends_with_a_fan_running():
    rng = np.random.default_rng(0)
    n = 16000 * 10
    w = rng.normal(0, 1, n)
    f = np.fft.rfft(w)
    f /= np.sqrt(np.arange(1, len(f) + 1))
    fan = np.fft.irfft(f, n)
    fan = (fan / fan.std() * 0.05).astype(np.float32)        # loud: webrtcvad called this speech for good
    x = np.concatenate([np.zeros(8000, np.float32), _voice(rng, 3.0), fan])
    cfg = config.load()
    cancel = threading.Event()

    class Audio:
        def subscribe(self):
            return _Feed(x, cancel)

        def unsubscribe(self, q):
            pass
    utt = vad.record_utterance(cfg, Audio(), cancel, start_timeout=3)
    assert utt is not None, "never stopped listening"
    assert len(utt.audio) / 16000 < 0.5 + 3.0 + 1.0           # ended within ~1 s of you stopping


def test_either_detector_wakes_on_either():
    from tesseract.wakeword import EitherDetector

    class D:
        def __init__(self, name, scores):
            self.name, self.scores, self.resets = name, iter(scores), 0

        def process(self, pcm):
            return next(self.scores)

        def reset(self):
            self.resets += 1
    a, b = D("franklin", [0.1, 0.9, 0.2]), D("sherpa", [0.0, 0.0, 1.0])
    both = EitherDetector(a, b)
    assert [both.process(None) for _ in range(3)] == [0.1, 0.9, 1.0] and both.name == "franklin + sherpa"
    both.reset()
    assert a.resets == b.resets == 1


def test_trained_model_needs_a_sustained_score():
    """A one-step spike (a syllable that looks like "Franklin") doesn't wake it; a held peak does."""
    from tesseract.wakeword import OpenWakeWordDetector
    model = Path(__file__).resolve().parent.parent / "models" / "franklin.onnx"
    feats = model.parent / "melspectrogram.onnx"
    if not (model.exists() and feats.exists()):
        pytest.skip("models not downloaded")
    det = OpenWakeWordDetector(model, model.parent, frames=2)
    scores = iter([0.1, 0.95, 0.1, 0.9, 0.92, 0.2])

    class FakeModel:
        def run(self, *_):
            return [np.array([[next(scores)]])]
    det.ww, det.warmup = FakeModel(), 0
    got = [det.process(np.zeros(1280, np.int16)) for _ in range(6)]
    assert got[:3] == [0.0, 0.1, 0.1]                     # the lone 0.95 never shows
    assert got[4] == pytest.approx(0.9)                  # two in a row: the lower of the two


@pytest.mark.skipif(not MODEL.exists(), reason="models/silero_vad.onnx not downloaded")
def test_a_long_explanation_survives_a_thinking_pause():
    rng = np.random.default_rng(1)
    gap = np.zeros(int(16000 * 1.3), np.float32)        # longer than end_silence_ms, shorter than long_pause_ms
    x = np.concatenate([np.zeros(8000, np.float32), _voice(rng, 10.0), gap, _voice(rng, 2.0),
                        np.zeros(16000 * 3, np.float32)])
    cfg = config.load()
    cancel = threading.Event()

    class Audio:
        def subscribe(self):
            return _Feed(x, cancel)

        def unsubscribe(self, q):
            pass
    utt = vad.record_utterance(cfg, Audio(), cancel, start_timeout=3)
    assert utt is not None, "never stopped listening"
    assert len(utt.audio) / 16000 > 0.5 + 10 + 1.3 + 1.5     # kept listening through the pause
    assert len(utt.audio) / 16000 < 0.5 + 10 + 1.3 + 2 + 2   # and still ended after the last words
