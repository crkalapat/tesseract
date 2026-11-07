import sys, glob, os, wave
import numpy as np
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from tesseract.wakeword import OpenWakeWordDetector, SherpaKeywordDetector
model, code, wavs = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
oww = OpenWakeWordDetector(Path(model), code / "models")
sh = SherpaKeywordDetector("Franklin", code / "models", 0.3, 0.5, ["Franken", "Franklyn", "Frankin#0.45"])
def load(p):
    with wave.open(p) as f: return np.frombuffer(f.readframes(f.getnframes()), np.int16).astype(np.float32) / 32768
def prep(x, snr, gain):
    r = np.random.default_rng(1); x = x / (np.abs(x).max() + 1e-9) * gain
    y = np.concatenate([np.zeros(32000, np.float32), x, np.zeros(16000, np.float32)])
    y = y + r.normal(0, np.sqrt(np.mean(x ** 2)) / 10 ** (snr / 20), len(y)).astype(np.float32)
    return np.clip(y * 32767, -32768, 32767).astype(np.int16)
res = {}
for f in sorted(glob.glob(f"{wavs}/*.wav")):
    for snr, g in ((20, 0.5), (10, 0.15)):
        pcm = prep(load(f), snr, g); oww.reset(); sh.reset()
        o = np.array([oww.process(pcm[i:i + 1280]) for i in range(0, len(pcm) - 1279, 1280)])
        s = max(sh.process(pcm[i:i + 1280]) for i in range(0, len(pcm) - 1279, 1280))
        res[(os.path.basename(f), snr)] = (o, s)
def fire(o, thr, k):
    ok = (o >= thr).astype(int)
    return (np.convolve(ok, np.ones(k, int), "full")[:len(ok)] >= k).any()
print(f"{'set-up':34s} {'mac 20dB':>9s} {'mac 10dB':>9s} {'sound-alikes 20dB':>18s}")
def row(name, f):
    p20 = np.mean([f(*v) for (n, s), v in res.items() if n.startswith("p_") and s == 20])
    p10 = np.mean([f(*v) for (n, s), v in res.items() if n.startswith("p_") and s == 10])
    n20 = sum(f(*v) for (n, s), v in res.items() if n.startswith("n_") and s == 20)
    print(f"{name:34s} {p20:9.0%} {p10:9.0%} {n20:11d}/176")
row("sherpa alone (now)", lambda o, s: s >= 1)
for thr, k in ((0.8, 1), (0.6, 2), (0.5, 2), (0.7, 2)):
    row(f"new alone, {k} frame(s) >= {thr}", lambda o, s, thr=thr, k=k: fire(o, thr, k))
    row(f"both, new at {k} frame(s) >= {thr}", lambda o, s, thr=thr, k=k: s >= 1 or fire(o, thr, k))
# which phrases does each catch? (20 dB)
phr = ["Franklin, what time is it", "Franklin", "Franken, what's the weather", "Frankin, set a timer",
       "Franklin set a timer for ten minutes", "Frankl'n, turn it up", "hey Franklin", "Frank lin, stop", "Frankn, what's up", "Franklen, volume up"]
print("\nper phrase at 20 dB (sherpa / new 2x0.6 / both):")
for i, p in enumerate(phr):
    vs = [v for (n, s), v in res.items() if n.startswith("p_") and n.endswith(f"_{i}.wav") and s == 20]
    a, b = sum(v[1] >= 1 for v in vs), sum(fire(v[0], 0.6, 2) for v in vs)
    c = sum(v[1] >= 1 or fire(v[0], 0.6, 2) for v in vs)
    print(f"  {p:40s} {a:2d} / {b:2d} / {c:2d}  of {len(vs)}")
