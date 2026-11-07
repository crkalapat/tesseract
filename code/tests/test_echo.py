"""Echo cancellation: Franklin's own voice comes back out of the mic, yours stays."""

import numpy as np

from tesseract.echo import B, EchoCanceller


def _speechlike(n, rng):
    """Noise shaped like speech: low-passed, in bursts of ~200 ms."""
    x = np.convolve(rng.normal(0, 1, n), np.hanning(9), "same")
    env = np.repeat(rng.uniform(0.1, 1.0, n // 3200 + 1), 3200)[:n]
    return (0.2 * x * env / np.abs(x).max()).astype(np.float32)


def _room(rng):
    ir = np.zeros(800, np.float32)
    ir[24] = 1.0
    for k in range(40):
        ir[30 + int(rng.integers(0, 700))] += rng.normal(0, 0.25) * np.exp(-k / 12)
    return ir


def run(far, mic, lead=3):
    """The mixer writes `lead` frames ahead of the mic, like the real card."""
    ec, out = EchoCanceller(), np.zeros(len(mic), np.float32)
    for k in range(len(mic) // B):
        ec.far(far[k * B:(k + 1) * B])
        j = k - lead
        if j >= 0:
            out[j * B:(j + 1) * B] = ec.process(mic[j * B:(j + 1) * B])
    return ec, np.concatenate([out[B:], np.zeros(B, np.float32)])      # the suppressor's 20 ms


def test_cancels_its_own_voice_and_keeps_yours():
    rng = np.random.default_rng(0)
    n = 16000 * 8
    far = _speechlike(n, rng)
    echo = np.concatenate([np.zeros(640, np.float32), np.convolve(far, _room(rng))[:n] * 3])[:n]
    you = np.zeros(n, np.float32)
    you[6 * 16000:7 * 16000] = _speechlike(16000, rng) * 0.3          # you, 1 s, well under its voice
    ec, out = run(far, echo + you)
    assert abs(ec.delay - 664) <= 16                                  # 640 + the direct path (24)
    late = slice(4 * 16000, 6 * 16000)
    erle = 10 * np.log10(np.sum(echo[late] ** 2) / np.sum(out[late] ** 2))
    assert erle > 20, erle
    s = slice(6 * 16000 + 1600, 7 * 16000 - 1600)
    kept = 10 * np.log10(np.sum(you[s] ** 2) / np.sum((out[s] - you[s]) ** 2))
    raw = 10 * np.log10(np.sum(you[s] ** 2) / np.sum(echo[s] ** 2))
    assert kept > raw + 15, (raw, kept)                               # you went from buried to clearly there


def test_transparent_when_nothing_plays():
    rng = np.random.default_rng(1)
    n = 16000 * 2
    mic = _speechlike(n, rng)
    ec, out = run(np.zeros(n, np.float32), mic)
    assert ec.delay is None
    s = slice(1600, n - 3200)
    assert np.allclose(out[s], mic[s], atol=1e-4)                     # unchanged (just 20 ms later)


def test_tape_and_resync_for_the_interrupt_test():
    """`tesseract interrupt-test` records mic, speaker and result with their sample positions, and a
    dropout makes it line up again instead of cancelling the wrong thing."""
    rng = np.random.default_rng(2)
    n = 16000 * 5
    far = _speechlike(n, rng)
    echo = np.concatenate([np.zeros(640, np.float32), np.convolve(far, _room(rng))[:n] * 3])[:n]
    ec = EchoCanceller()
    ec.tape = {"far": [], "mic": [], "out": []}
    for k in range(n // B):
        ec.far(far[k * B:(k + 1) * B])
        if k >= 3:
            ec.process(echo[(k - 3) * B:(k - 2) * B])
    assert ec.delay is not None and ec.delay_ms > 0
    assert ec.tape["far"][0][0] == 0 and ec.tape["mic"][1][0] == B and len(ec.tape["out"]) == len(ec.tape["mic"])
    ec.discontinuity()
    ec.process(np.zeros(B, np.float32))
    assert ec.delay is None and ec.resyncs == 1


def test_stays_stable_in_a_loud_little_box():
    """Shaped like the real cube's recording: the mic hears the speaker 14x (+23 dB) louder than it was
    sent, the speaker distorts, and there are pauses between sentences. The old filter blew up here
    (its output got 35 dB louder than its input); now it must never make things worse."""
    rng = np.random.default_rng(3)
    n = 16000 * 12
    far = _speechlike(n, rng) * 0.15
    for s in range(16000, n, 16000 * 3):          # a pause after every sentence
        far[s:s + 8000] = 0
    echo = np.tanh(np.convolve(far, _room(rng))[:n] * 14 * 1.5) / 1.5      # +23 dB, then the speaker distorts
    echo = np.concatenate([np.zeros(640, np.float32), echo.astype(np.float32)])[:n]
    mic = echo + rng.normal(0, 1e-3, n).astype(np.float32)
    ec, out = run(far, mic)
    for s in range(2, 12, 2):
        sl = slice(s * 16000, (s + 2) * 16000)
        gained = 10 * np.log10(np.sum(mic[sl] ** 2) / np.sum(out[sl] ** 2))
        assert gained > -0.5, (s, gained)           # never louder than what came in
    late = slice(6 * 16000, 12 * 16000)
    assert 10 * np.log10(np.sum(mic[late] ** 2) / np.sum(out[late] ** 2)) > 6


def test_tells_its_own_voice_from_someone_talking_over_it():
    """The wake word firing while it talks: only a person adds sound its echo can't explain."""
    rng = np.random.default_rng(4)
    n = 16000 * 8
    far = _speechlike(n, rng)
    echo = np.concatenate([np.zeros(640, np.float32), np.convolve(far, _room(rng))[:n] * 3])[:n]
    you = np.zeros(n, np.float32)
    you[int(6.5 * 16000):int(7.3 * 16000)] = _speechlike(int(0.8 * 16000), rng) * 0.4
    ec = EchoCanceller()
    said = {}
    for k in range(n // B):
        ec.far(far[k * B:(k + 1) * B])
        if k >= 3:
            j = (k - 3) * B
            ec.process(echo[j:j + B] + you[j:j + B])
            if j in (6 * 16000, int(7.4 * 16000)):           # "would it count as a person?" at these moments
                said[j] = ec.someone_talked()
    assert ec.playing_recently()
    assert said[6 * 16000] is False                         # just its own voice: a wake here is ignored
    assert said[int(7.4 * 16000)] is True                   # you talked over it: a wake here counts


def test_checks_from_the_frame_the_wake_word_fired_on():
    """On the Pi the wake word runs behind the mic, and the check later still: judged from "now", a real
    "Franklin" had already scrolled out of the window and was ignored. Judged from its own frame, it counts."""
    rng = np.random.default_rng(5)
    n = 16000 * 10
    far = _speechlike(n, rng)
    echo = np.concatenate([np.zeros(640, np.float32), np.convolve(far, _room(rng))[:n] * 3])[:n]
    you = np.zeros(n, np.float32)
    you[int(6.0 * 16000):int(6.8 * 16000)] = _speechlike(int(0.8 * 16000), rng) * 0.4
    ec = EchoCanceller()
    fired_at = None
    for k in range(n // B):
        ec.far(far[k * B:(k + 1) * B])
        if k >= 3:
            j = (k - 3) * B
            ec.process(echo[j:j + B] + you[j:j + B])
            if j == int(6.9 * 16000):
                fired_at = j // B                          # the wake word fired on this frame...
    assert ec.someone_talked(at=fired_at) is True          # ...checked 3 s later, still counts as you
    assert ec.someone_talked() is False                    # (from "now" it would have been ignored)


def test_mic_frames_are_numbered():
    from tesseract.audio import MicFrame
    f = np.zeros(320, np.int16).view(MicFrame)
    f.index = 7
    assert f.index == 7 and np.concatenate([f, f]).shape == (640,)


def test_a_reply_saying_franklin_can_still_be_interrupted():
    """A reply that says the wake word used to block voice interruptions until it finished. Now a person
    talking over it (per the echo canceller) gets through; its own voice alone still doesn't."""
    import time as _time
    from types import SimpleNamespace
    from tesseract.app import Tesseract

    app = Tesseract.__new__(Tesseract)
    app.wake = SimpleNamespace(wake_position=123)
    app.speech = SimpleNamespace(says_wake_word=True, finished_at=float("inf"))   # still talking
    heard = {}

    class Echo:
        talked = False
        def playing_recently(self): return True
        def someone_talked(self, at=None):
            heard["at"] = at
            return self.talked

    app.audio = SimpleNamespace(echo=Echo())
    assert app._hearing_itself()                 # only its own voice: ignore the wake
    app.audio.echo.talked = None
    assert app._hearing_itself()                 # can't tell (not calibrated yet): ignore, as before
    app.audio.echo.talked = True
    assert not app._hearing_itself()             # someone said "Franklin" over it: interrupt
    assert heard["at"] == 123
    app.audio = SimpleNamespace(echo=None)
    assert app._hearing_itself()                 # no echo canceller: the old rule
    app.speech = SimpleNamespace(says_wake_word=True, finished_at=_time.monotonic() - 5)
    assert not app._hearing_itself()             # finished a while ago
