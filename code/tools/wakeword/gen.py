"""Synthesize "Franklin" training data with Piper (904 speakers) and turn it into openWakeWord windows.

Writes data.npz with:
  pos_train, pos_eval   positive windows [N,16,96] (word just ended), eval = held-out speakers
  neg_tts               windows from sound-alike words/phrases and noise (never "Franklin")
  pos_eval_clips        per held-out clip: list of window arrays around the word end (for clip recall)
"""

import multiprocessing as mp
import os
import sys
import unicodedata

import numpy as np

CODE = sys.argv[1]
OUT = sys.argv[2]
N_POS, N_NEG = int(sys.argv[3]), int(sys.argv[4])
sys.path.insert(0, CODE)

POS = [  # (IPA, weight)
    ("fɹˈæŋklɪn.", 6), ("fɹˈæŋklɪn,", 6), ("fɹˈæŋklɪn!", 3), ("fɹˈæŋklɪn?", 3),
    ("hˈeɪ fɹˈæŋklɪn.", 2), ("oʊkˈeɪ fɹˈæŋklɪn,", 1), ("fɹæŋklˈɪn,", 1),
    # said quickly, the "lin" goes soft
    ("fɹˈæŋkən,", 3), ("fɹˈæŋkɪn,", 2), ("fɹˈæŋklən.", 3), ("fɹˈæŋkəln,", 2), ("fɹˈæŋkln.", 1),
]
NEG = [  # sound-alikes, near misses and common words
    "fɹˈæŋk.", "fɹˈæŋk, kˈʌm hˈɪɹ.", "fɹˈæŋkli.", "fɹˈæŋkli, aɪ dˈoʊnt kˈɛɹ.", "fɹˈæŋkənstˌaɪn.",
    "fɹˈæŋkɪnsˌɛns.", "fɹˈɛntʃ lˈɪnɪn.", "fɹˈæŋkoʊ.", "fɹˈænsɪs.", "lˈɪŋkən.", "kɹˈæŋk.", "kɹˈæŋkɪŋ.",
    "ɹˈæŋkɪn.", "bˈɛndʒəmɪn.", "blˈæŋkɪt.", "pˈʌmpkɪn.", "fɹˈæŋkfɚt.", "fɹˈæntʃaɪz.", "fɹˈæŋkfɚɾɚ.",
    "plˈæŋkɪŋ.", "θˈæŋk juː.", "θˈæŋks ɐ lˈɑːt.", "dɹˈɪŋkɪŋ.", "fɹˈæni.", "fɹˈæn.", "fɹˈæŋki.",
    "kɹˈɛmlɪn.", "ɡɹˈɛmlɪn.", "ˈæŋklɪt.", "ɹˈæŋklɪŋ.", "hˈæmlɪn.", "fɹˈɛnd.", "fɹˈɛndli.", "fɹˈʌm.",
    "fɹˈiː.", "fɹˈaɪdeɪ.", "flˈæŋk.", "flˈæʃlaɪt.", "ɪn ɐ fɹˈæŋk wˈeɪ.", "ðə fɹˈæŋk ænd ˈoʊpən tˈɔːk.",
    "fɹˈæŋk sˈɪnætɹə.", "mˈɪstɚ fɹˈæŋk.", "tˈɜːn ɪt ˈʌp.", "wˌʌts ðə wˈɛðɚ?", "sˈɛt ɐ tˈaɪmɚ.",
    "plˈeɪ sʌm mjˈuːzɪk.", "ɡʊd nˈaɪt.", "hˈɛloʊ ðˈɛɹ.", "aɪ θˈɪŋk sˈoʊ.", "wˈɛl, ˈæktʃuəli, nˈoʊ.",
]
RATE = 16000
CHUNK = 1280


def worker_init(code, voice_path, models):
    global VOICE, SC, MEL, EMB, RS
    sys.path.insert(0, code)
    import onnxruntime as ort
    from piper import PiperVoice, SynthesisConfig

    from tesseract import dsp
    VOICE, SC = PiperVoice.load(voice_path), SynthesisConfig
    o = ort.SessionOptions()
    o.intra_op_num_threads = o.inter_op_num_threads = 1
    o.log_severity_level = 3
    MEL = ort.InferenceSession(f"{models}/melspectrogram.onnx", o, providers=["CPUExecutionProvider"])
    EMB = ort.InferenceSession(f"{models}/embedding_model.onnx", o, providers=["CPUExecutionProvider"])
    RS = dsp.StreamResampler


def say(ipa, rng, speaker):
    ids = VOICE.phonemes_to_ids(list(unicodedata.normalize("NFD", ipa)))
    cfg = SC(speaker_id=speaker, length_scale=float(rng.uniform(0.75, 1.35)),
             noise_scale=float(rng.uniform(0.4, 0.9)), noise_w_scale=float(rng.uniform(0.5, 1.0)))
    a = np.asarray(VOICE.phoneme_ids_to_audio(ids, cfg), dtype=np.float32)
    return RS(22050, RATE).process(a)


def trim(a):
    """(audio, start, end) of the part above -35 dB of its peak."""
    e = np.sqrt(np.convolve(a ** 2, np.ones(160) / 160, "same"))
    on = np.flatnonzero(e > e.max() * 10 ** (-35 / 20))
    return a, int(on[0]), int(on[-1])


def noise(rng, n):
    kind = rng.choice(["pink", "white", "brown", "hum", "quiet", "babble"], p=[.3, .1, .15, .1, .15, .2])
    if kind == "quiet":
        return rng.normal(0, 1, n) * 0.05
    if kind == "hum":
        t = np.arange(n) / RATE
        return np.sin(2 * np.pi * rng.choice([50, 60, 100, 120]) * t) + rng.normal(0, 0.3, n)
    if kind == "babble":
        out = np.zeros(n)
        for _ in range(3):
            b = say(NEG[rng.integers(len(NEG))], rng, int(rng.integers(904)))
            s = int(rng.integers(0, max(1, n - len(b))))
            out[s:s + len(b)] += b[: n - s]
        return out + rng.normal(0, 0.02, n)
    w = rng.normal(0, 1, n)
    f = np.fft.rfft(w)
    p = {"pink": 0.5, "white": 0.0, "brown": 1.0}[kind]
    f /= np.arange(1, len(f) + 1) ** p
    return np.fft.irfft(f, n)


def room(rng, a):
    if rng.random() < 0.5:
        rt = rng.uniform(0.1, 0.5)
        n = int(rt * RATE)
        ir = rng.normal(0, 1, n) * np.exp(-6.9 * np.arange(n) / n)
        ir[0] = 1 / rng.uniform(0.2, 1.0)
        a = np.convolve(a, ir / np.abs(ir).sum() * 4)[: len(a) + n // 2]
    if rng.random() < 0.5:                          # a cheap mic: one-pole low-pass or high-pass
        k = rng.uniform(0.2, 0.8)
        y = np.copy(a)
        for i in range(1, len(y)):
            y[i] = k * y[i - 1] + (1 - k) * a[i]
        a = y if rng.random() < 0.5 else a - y
    return a


def windows(clip):
    """Exactly what tesseract/wakeword.py's OpenWakeWordDetector sees: (end sample, [16,96]) per 80 ms."""
    x = np.clip(clip * 32767, -32768, 32767).astype(np.int16).astype(np.float32)
    raw = np.zeros(480, np.float32)
    mel_buf = np.ones((76, 32), np.float32)
    feats = np.zeros((16, 96), np.float32)
    out = []
    for k, i in enumerate(range(0, len(x) - CHUNK + 1, CHUNK)):
        raw = np.concatenate((raw, x[i:i + CHUNK]))[-(CHUNK + 480):]
        mel = np.squeeze(MEL.run(None, {"input": raw[None, :]})[0]) / 10 + 2
        mel_buf = np.vstack((mel_buf, mel))[-76:]
        emb = EMB.run(None, {"input_1": mel_buf[None, :, :, None]})[0].reshape(-1)
        feats = np.vstack((feats[1:], emb))
        if k >= 20:                                 # the detector's warm-up
            out.append((i + CHUNK, feats.copy()))
    return out


def make(job):
    kind, seed, speaker = job
    rng = np.random.default_rng(seed)
    if kind == "neg_noise":
        n = int(rng.uniform(3, 6) * RATE)
        clip = noise(rng, n)
        clip = clip / (np.abs(clip).max() + 1e-9) * 10 ** (rng.uniform(-40, -10) / 20)
        return kind, [w for _, w in windows(clip)], None
    if kind.startswith("pos"):
        ipa = POS[rng.choice(len(POS), p=np.array([w for _, w in POS]) / sum(w for _, w in POS))][0]
    else:
        ipa = NEG[rng.integers(len(NEG))]
    word, s, e = trim(say(ipa, rng, speaker))
    pre = int(rng.uniform(1.7, 2.4) * RATE)
    post = int(rng.uniform(0.4, 0.8) * RATE)
    clip = np.concatenate((np.zeros(pre), word[s:], np.zeros(post)))
    w_start, w_end = pre, pre + (e - s)
    clip = room(rng, clip)[: len(clip)]
    sig = np.sqrt(np.mean(word[s:e] ** 2)) + 1e-9
    bg = noise(rng, len(clip))
    bg = bg / (np.sqrt(np.mean(bg ** 2)) + 1e-9) * sig * 10 ** (-rng.uniform(3, 30) / 20)
    clip = clip + bg
    clip = clip / (np.abs(clip).max() + 1e-9) * 10 ** (rng.uniform(-30, -3) / 20)
    ws = windows(clip)
    if kind.startswith("neg"):
        return kind, [w for _, w in ws], None
    pos = [w for end, w in ws if w_end <= end <= w_end + int(0.32 * RATE)]
    early = [w for end, w in ws if end <= w_start + int(0.35 * (w_end - w_start))]   # silence, or only "Fr-"
    return kind, pos, early


if __name__ == "__main__":
    voice, models = sys.argv[5], sys.argv[6]
    eval_spk = set(range(0, 904, 9))                 # every 9th speaker is never trained on
    train_spk = [s for s in range(904) if s not in eval_spk]
    rng = np.random.default_rng(0)
    jobs = [("pos_train", i, int(rng.choice(train_spk))) for i in range(N_POS)]
    jobs += [("pos_eval", 10**6 + i, int(rng.choice(sorted(eval_spk)))) for i in range(N_POS // 8)]
    jobs += [("neg_tts", 2 * 10**6 + i, int(rng.integers(904))) for i in range(N_NEG)]
    jobs += [("neg_noise", 3 * 10**6 + i, 0) for i in range(N_NEG // 6)]
    out = {"pos_train": [], "pos_eval": [], "neg_tts": [], "pos_eval_clips": []}
    with mp.Pool(os.cpu_count(), initializer=worker_init, initargs=(CODE, voice, models)) as pool:
        for n, (kind, ws, early) in enumerate(pool.imap_unordered(make, jobs, chunksize=8)):
            if kind == "pos_train":
                out["pos_train"] += ws
                out["neg_tts"] += early
            elif kind == "pos_eval":
                out["pos_eval"] += ws
                out["pos_eval_clips"].append(len(ws))
            else:
                out["neg_tts"] += ws
            if n % 2000 == 0:
                print(n, "/", len(jobs), flush=True)
    np.savez_compressed(OUT, **{k: np.array(v, np.float32) for k, v in out.items()})
    print({k: len(v) for k, v in out.items()})
