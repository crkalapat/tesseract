"""The wake word keeps up with the mic: no late wakes "in silence", no double fire from a backlog."""

import queue
import threading
import time

import numpy as np

from tesseract import config, wakeword
from tesseract.audio import MicFrame


class FakeAudio:
    muted = False

    def __init__(self):
        self.frames_out = 0
        self.qs = []

    backlog: list = []

    def subscribe(self):
        q = queue.Queue()
        for f in self.backlog:          # audio that piled up before the listener got to it
            q.put(f)
        self.qs.append(q)
        return q

    def queue_up(self, n, loud=False):
        for _ in range(n):
            f = np.full(320, 1000 if loud else 0, np.int16).view(MicFrame)
            f.index = self.frames_out
            self.frames_out += 1
            self.backlog = self.backlog + [f]

    def push(self, n, loud=False):
        for _ in range(n):
            f = np.full(320, 1000 if loud else 0, np.int16).view(MicFrame)
            f.index = self.frames_out
            self.frames_out += 1
            for q in self.qs:
                q.put(f)


class Det:
    """Fires on loud audio; optionally slow."""
    def __init__(self, name, delay=0.0):
        self.name, self.delay, self.calls = name, delay, 0

    def process(self, pcm):
        self.calls += 1
        time.sleep(self.delay)
        return 1.0 if pcm.max() > 0 else 0.0

    def reset(self):
        pass


def make(monkeypatch, *dets):
    monkeypatch.setattr(wakeword, "make_detector",
                        lambda cfg: wakeword.EitherDetector(*dets) if len(dets) > 1 else dets[0])
    audio, fired = FakeAudio(), []
    w = wakeword.WakeWordListener(config.load(), audio, on_wake=fired.append)
    return w, audio, fired


def wait(cond, t=3.0):
    end = time.monotonic() + t
    while time.monotonic() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_each_detector_gets_its_own_thread(monkeypatch):
    w, audio, fired = make(monkeypatch, Det("a"), Det("b"))
    w.start()
    assert len(w._threads) == 2 and len(audio.qs) == 2
    audio.push(10, loud=True)
    assert wait(lambda: fired)
    time.sleep(0.2)
    assert len(fired) == 1                                   # both heard it: one wake
    w.stop()


def test_two_backlogged_wakes_dont_both_fire(monkeypatch):
    """Said twice 0.5 s apart but processed in one burst: one wake (the refractory is in audio time)."""
    w, audio, fired = make(monkeypatch, Det("a"))
    d = w._parts[0]
    w._fire(d, 1.0, pos=100, behind=0)
    w._fire(d, 1.0, pos=125, behind=0)                       # 0.5 s of audio later, a moment later in real time
    w._fire(d, 1.0, pos=180, behind=0)                       # 1.6 s later: a new "Franklin"
    w._fire(d, 1.0, pos=300, behind=90)                      # 1.8 s old by the time it's seen: too late
    assert len(fired) == 2 and w.wake_position == 180


def test_skips_ahead_instead_of_waking_late(monkeypatch):
    """Seconds behind: the old audio is dropped, so a "Franklin" from 3 s ago doesn't wake it now."""
    w, audio, fired = make(monkeypatch, Det("a"))
    audio.queue_up(5, loud=True)                             # said 3 s ago...
    audio.queue_up(150)                                      # ...and the listener is only getting to it now
    w.start()
    time.sleep(0.5)
    assert fired == [] and w.skips == 1
    audio.push(5, loud=True)                                 # said now: heard right away
    assert wait(lambda: fired)
    w.stop()
