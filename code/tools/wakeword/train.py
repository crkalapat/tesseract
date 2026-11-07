"""Train the "Franklin" openWakeWord classifier and measure it.

  python train.py data.npz validation_set_features.npy CODE_DIR WAKE_WAV_DIR out.onnx
"""

import glob
import os
import sys
import wave

import numpy as np
import torch
from torch import nn

data, negfile, CODE, WAVS, OUT = sys.argv[1:6]
sys.path.insert(0, CODE)
torch.manual_seed(0)
rng = np.random.default_rng(0)

d = np.load(data)
pos_tr, pos_ev, neg_tts = d["pos_train"], d["pos_eval"], d["neg_tts"]
clips = d["pos_eval_clips"].astype(int)
real = np.load(negfile, mmap_mode="r")
split = int(len(real) * 0.7)                       # the rest (~3.2 h) is never trained on
def real_windows(lo, hi, stride):
    idx = np.arange(lo + 15, hi, stride)
    return np.stack([real[i - 15:i + 1] for i in idx]).astype(np.float32)
real_tr = real_windows(0, split, 1)
print(f"train: {len(pos_tr)} positive windows, {len(neg_tts)} synthetic negative, {len(real_tr)} real-audio negative")


class Net(nn.Module):
    """openWakeWord's small DNN: 16x96 embeddings -> 128 -> 128 -> score."""
    def __init__(self):
        super().__init__()
        self.f = nn.Sequential(nn.Flatten(), nn.Linear(16 * 96, 128), nn.LayerNorm(128), nn.ReLU(),
                               nn.Linear(128, 128), nn.LayerNorm(128), nn.ReLU(), nn.Linear(128, 1))

    def forward(self, x):
        return torch.sigmoid(self.f(x))


X = np.concatenate([pos_tr, neg_tts, real_tr])
y = np.concatenate([np.ones(len(pos_tr)), np.zeros(len(neg_tts)), np.zeros(len(real_tr))]).astype(np.float32)
w = np.concatenate([np.ones(len(pos_tr)), np.full(len(neg_tts), 1.0), np.full(len(real_tr), 3.0)]).astype(np.float32)
w[y == 1] *= (w[y == 0].sum() / w[y == 1].sum())  # balance the classes
Xt, yt, wt = torch.tensor(X), torch.tensor(y), torch.tensor(w)
net = Net()
opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-3)
EPOCHS = 40
sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=2e-3, total_steps=EPOCHS * (len(X) // 1024 + 1))
for epoch in range(EPOCHS):
    perm = torch.randperm(len(X))
    net.train()
    for i in range(0, len(X), 1024):
        b = perm[i:i + 1024]
        p = net(Xt[b]).squeeze(1).clamp(1e-6, 1 - 1e-6)
        loss = (nn.functional.binary_cross_entropy(p, yt[b], reduction="none") * wt[b]).mean()
        opt.zero_grad()
        loss.backward()
        opt.step()
        sched.step()
    if epoch in (10, 18, 26, 33):                          # hard negatives: whatever still scores high counts double
        with torch.no_grad():
            s = net(Xt).squeeze(1).numpy()
        w[(y == 0) & (s > 0.2)] *= 2
        wt = torch.tensor(w)
net.eval()
score = lambda a: net(torch.tensor(np.asarray(a, np.float32))).detach().numpy().reshape(-1)

# ── evaluation ─────────────────────────────────────────────────────────────
ev = score(pos_ev)
starts = np.concatenate([[0], np.cumsum(clips)[:-1]])
clip_max = np.array([ev[s:s + n].max() if n else 0 for s, n in zip(starts, clips)])
real_ev = real_windows(split, len(real), 1)
s_real = score(real_ev)
hours = (len(real) - split) * 0.08 / 3600


def false_wakes(s, thr):
    """Count separate firings (1.5 s refractory, like the listener)."""
    n, last = 0, -999
    for i in np.flatnonzero(s >= thr):
        if i - last > 19:
            n, last = n + 1, i
    return n


# the macOS voices (never seen in training), streamed through the real detector like on the Pi
from tesseract.wakeword import OpenWakeWordDetector  # noqa: E402

torch.onnx.export(net, torch.zeros(1, 16, 96), OUT, input_names=["input"], output_names=["score"],
                  dynamic_axes={"input": {0: "batch"}, "score": {0: "batch"}}, opset_version=13, dynamo=False)
