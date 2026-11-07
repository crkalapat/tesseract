# How models/franklin.onnx was made

An openWakeWord-style classifier (16 x 96 embeddings -> 128 -> 128 -> score) on top of openWakeWord's
frozen feature models, trained on a laptop in about 20 minutes. Not used on the Pi; kept so the model
can be rebuilt or retrained for another name.

1. **Positives:** `gen.py` speaks "Franklin" (and the way it comes out said fast: "Franken",
   "Franklen", ...) with Piper's `en_US-libritts_r-medium` voice: 6,000 clips from 800 of its 904
   speakers, random speed and pitch variation, synthetic room echo, a cheap-mic filter, background
   noise at 3-30 dB SNR, and random levels. Every 9th speaker is held out for evaluation. The text is
   written as phonemes (IPA) because Piper's macOS wheel can't find its espeak-ng data.
2. **Negatives:** sound-alikes and near misses ("Frank", "frankly", "Frankenstein", "Lincoln",
   "Kremlin", ...) the same way, noise clips, and 70% of openWakeWord's
   [validation features](https://huggingface.co/datasets/davidscripka/openwakeword_features)
   (10.7 h of real speech, music and noise; the other 30% is only used to count false wakes).
3. **Features** go through exactly what `tesseract/wakeword.py` computes on the Pi (80 ms steps,
   1.28 s windows), labelled positive for the windows just after the word ends.
4. **Training** (`train.py`): AdamW, class-balanced, real audio weighted 3x, four rounds of
   doubling the weight of negatives that still score high. Exported to ONNX.

```bash
pip install torch piper-tts onnx onnxruntime numpy
# voice: huggingface.co/rhasspy/piper-voices en/en_US/libritts_r/medium (onnx + json)
python gen.py ../.. data.npz 6000 5000 en_US-libritts_r-medium.onnx ../../models
python train.py data.npz validation_set_features.npy ../.. WAVS_DIR franklin.onnx
python compare.py franklin.onnx ../.. WAVS_DIR     # against sherpa, on the same audio
```

Measured (Oct 2026): held-out Piper speakers 82% caught at the shipped setting (score >= 0.6 for 2
steps), 0.6 false wakes per hour of real-world audio. Together with sherpa, 72% of a hard set of
macOS voices in a quiet room and 64% in a noisy one, against 56% and 55% for sherpa alone.
