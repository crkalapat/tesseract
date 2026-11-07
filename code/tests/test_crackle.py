"""Pops and crackle: a late network chunk fades instead of cutting, loud peaks round off instead of clipping."""

import numpy as np

from tesseract.playback import StreamSource, soft_limit

SR = 48_000


def tone(seconds, amp=0.5):
    t = np.arange(int(SR * seconds)) / SR
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_waits_for_a_small_cushion_then_plays():
    s = StreamSource()
    s.push(tone(0.05))
    assert not s.ready() and len(s.read(960)) == 0          # 50 ms isn't enough to start on
    s.push(tone(0.2))
    assert s.ready() and len(s.read(960)) == 960


def test_running_dry_mid_word_fades_instead_of_popping():
    s = StreamSource()
    s.push(tone(0.2))
    out = []
    while True:
        x = s.read(960)
        if not len(x):
            break
        out.append(x)
    played = np.concatenate(out)
    assert abs(played[-1]) < 0.01 and s.dry_count == 1     # faded to zero, not cut at full swing
    s.push(tone(0.1))
    assert len(s.read(960)) == 0                             # waits for a bigger cushion before resuming
    s.push(tone(0.3))
    back = s.read(960)
    assert abs(back[0]) < 0.01 and abs(back[-1]) > 0.0       # and fades back in


def test_the_pause_between_sentences_isnt_a_dropout():
    s = StreamSource()
    x = tone(0.2)
    x[-2400:] = 0                                            # a sentence ends in silence
    s.push(x)
    while len(s.read(960)):
        pass
    assert s.dry_count == 0


def test_loud_peaks_round_off():
    x = np.array([0.1, 0.5, 0.79, 0.95, 1.4, -2.0], np.float32)
    y = soft_limit(x)
    assert np.allclose(y[:3], x[:3])                         # untouched below the knee
    assert np.all(np.abs(y) < 1.0) and np.all(np.diff(np.abs(y[2:5])) > 0)   # still rising, never flat-topped
