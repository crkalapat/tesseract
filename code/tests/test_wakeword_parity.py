"""Checks our lightweight openWakeWord runner against the official package.

Needs `pip install openwakeword` (dev machine only) and macOS `say` or espeak to
synthesize a test phrase. Run:  python -m pytest tests/test_wakeword_parity.py -s
"""

import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
import pytest

from tesseract import dsp
from tesseract.wakeword import FEATURE_MODELS, OpenWakeWordDetector, ensure_model_files

MODELS = Path(__file__).resolve().parent.parent / "models"


def synth(text: str) -> np.ndarray:
    tmp = Path(tempfile.mkdtemp())
    if shutil.which("say"):
        subprocess.run(["say", "-o", str(tmp / "a.wav"), "--data-format=LEI16@16000", text], check=True)
    elif shutil.which("espeak-ng"):
        subprocess.run(["espeak-ng", "-w", str(tmp / "a.wav"), text], check=True)
    else:
        pytest.skip("no speech synthesizer")
    with wave.open(str(tmp / "a.wav")) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
        rate = w.getframerate()
    if rate != 16000:
        x = dsp.StreamResampler(rate, 16000).process(x)
    return x.astype(np.int16)


def test_parity_with_official_openwakeword():
    oww = pytest.importorskip("openwakeword.model")
    ensure_model_files(MODELS, [*FEATURE_MODELS, "hey_jarvis_v0.1.onnx"])

    speech = synth("Hey Jarvis")
    silence = np.zeros(16000 * 2, dtype=np.int16)
    noise = (np.random.default_rng(0).normal(0, 30, 16000 * 2)).astype(np.int16)
    audio = np.concatenate((noise, silence, speech, silence))
    audio = audio[: len(audio) // 1280 * 1280]

    ours = OpenWakeWordDetector(MODELS / "hey_jarvis_v0.1.onnx", MODELS)
    ref = oww.Model(wakeword_models=[str(MODELS / "hey_jarvis_v0.1.onnx")], inference_framework="onnx",
                    melspec_model_path=str(MODELS / "melspectrogram.onnx"),
                    embedding_model_path=str(MODELS / "embedding_model.onnx"))

    a, b = [], []
    for i in range(0, len(audio), 1280):
        chunk = audio[i:i + 1280]
        a.append(ours.process(chunk))
        b.append(list(ref.predict(chunk).values())[0])
    a, b = np.array(a), np.array(b)

    print(f"\nours max {a.max():.3f} at {a.argmax()}, official max {b.max():.3f} at {b.argmax()}")
    assert b.max() > 0.5, "official model didn't detect the synthesized phrase; test audio is bad"
    assert a.max() > 0.5
    assert abs(int(a.argmax()) - int(b.argmax())) <= 1
    # After both have warmed up (2 s), scores should agree closely.
    assert np.max(np.abs(a[25:] - b[25:])) < 0.05


def test_no_trigger_on_silence_and_noise():
    ensure_model_files(MODELS, [*FEATURE_MODELS, "hey_jarvis_v0.1.onnx"])
    det = OpenWakeWordDetector(MODELS / "hey_jarvis_v0.1.onnx", MODELS)
    rng = np.random.default_rng(1)
    audio = rng.normal(0, 300, 16000 * 5).astype(np.int16)
    assert det.process(audio) < 0.2
