"""The default wake engine: sherpa-onnx keyword spotting for "Franklin", no trained model.

Needs macOS `say` for test speech (skipped elsewhere) and downloads the 17 MB keyword model once.
"""

import shutil
import subprocess
import wave

import numpy as np
import pytest

from tesseract import config
from tesseract.wakeword import SherpaKeywordDetector


def speech(tmp_path, text, voice):
    if not shutil.which("say"):
        pytest.skip("needs macOS `say`")
    p = tmp_path / "s.wav"
    subprocess.run(["say", "-v", voice, "-o", str(p), "--data-format=LEI16@16000", text], check=True)
    with wave.open(str(p)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    pad = np.zeros(8000, np.int16)
    return np.concatenate([pad, x, pad, pad])


def detections(det, pcm):
    det.reset()
    return sum(det.process(pcm[i:i + 320]) for i in range(0, len(pcm), 320))


@pytest.fixture(scope="module")
def detector():
    cfg = config.load()
    w = cfg.wake
    return SherpaKeywordDetector(w.keyword or cfg.assistant.name, cfg.path("models"),
                                 w.keyword_threshold, w.keyword_boost)


def test_tokens(detector):
    assert detector.tokens == "▁F RA N K LI N"


@pytest.mark.parametrize("voice", ["Samantha", "Daniel", "Karen", "Moira"])
def test_hears_franklin(tmp_path, detector, voice):
    assert detections(detector, speech(tmp_path, "Franklin, what time is it?", voice)) == 1


@pytest.mark.parametrize("text", ["Frankly, I don't know.", "Frank, can you pass the salt?",
                                  "We watched Frankenstein last night.", "I bought French linen sheets."])
def test_ignores_sound_alikes(tmp_path, detector, text):
    assert detections(detector, speech(tmp_path, text, "Samantha")) == 0
