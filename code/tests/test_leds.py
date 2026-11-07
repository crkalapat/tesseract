import numpy as np

from tesseract import config
from tesseract.leds import Leds
from tesseract.state import State, Status


def test_every_state_renders():
    cfg = config.load()
    st = Status()
    leds = Leds(cfg, st, mic_level=lambda: 0.7, speaker_level=lambda: 0.5, sim=True)
    n = cfg.leds.count
    for state in State:
        st.set(state=state, detail="Pasta timer")
        for t in np.linspace(0, 3, 40):
            f = leds._scene(st, t, t)
            assert f.shape == (n, 3), state
            f = np.clip(leds._overlays(st, np.clip(f, 0, 1), t), 0, 1)
            assert np.isfinite(f).all()


def test_state_looks():
    cfg = config.load()
    st = Status()
    leds = Leds(cfg, st, mic_level=lambda: 0.0, speaker_level=lambda: 0.0, sim=True)
    st.set(state=State.IDLE)
    assert leds._scene(st, 1.0, 1.0).max() == 0          # dark when idle (bedroom)
    st.set(muted=True)
    f = leds._scene(st, 1.0, 1.0)
    assert (f[:, 0] > f[:, 2]).all()                     # red while muted
    st.set(muted=False, state=State.LISTENING)
    quiet = leds._scene(st, 1.0, 1.0).sum()
    leds.mic_level = lambda: 1.0
    for _ in range(20):
        loud = leds._scene(st, 1.0, 1.0).sum()
    assert loud > quiet * 1.5                            # swells with your voice


def _idle(**kw):
    cfg = config.load()
    st = Status()
    st.set(state=State.IDLE, **kw)
    return cfg, st, Leds(cfg, st, sim=True)


def test_background_jobs():
    import time
    # progress bar: more of the strip lit as the job gets further
    _, st, leds = _idle(task_state="running", task_progress=0.25)
    early = (leds._scene(st, 1.0, 1.0).max(axis=1) > 0.2).sum()
    st.set(task_progress=0.75)
    late = (leds._scene(st, 1.0, 1.0).max(axis=1) > 0.2).sum()
    assert 0 < early < late < len(leds.x)
    # needs you: one amber light that moves along the strip
    st.set(task_state="waiting")
    a, b = leds._scene(st, 0.5, 0.5), leds._scene(st, 1.0, 1.0)
    assert a.argmax(axis=0)[0] != b.argmax(axis=0)[0] and (a[:, 0] >= a[:, 2]).all()
    # done: the whole strip breathes green; failed: red
    st.set(task_state="done")
    lo, hi = leds._scene(st, 0.0, 0.0), leds._scene(st, 1.5, 1.5)
    assert hi[:, 1].min() > lo[:, 1].max() and (hi[:, 1] > hi[:, 0]).all()
    st.set(task_state="failed")
    f = leds._scene(st, 1.5, 1.5)
    assert (f[:, 0] > 5 * f[:, 1]).all()
    # a job outranks a timer; with no job the timer shows, dim, and drains
    st.set(task_state="", timers=[("pasta", time.time() + 300, 600)])
    half = leds._scene(st, 1.0, 1.0)
    st.set(timers=[("pasta", time.time() + 60, 600)])
    tenth = leds._scene(st, 1.0, 1.0)
    assert 0 < (tenth.max(axis=1) > 0.05).sum() < (half.max(axis=1) > 0.05).sum()
    assert half.max() <= 0.35                              # noticeable, not attention-grabbing
    st.set(muted=True)
    assert (leds._scene(st, 1.0, 1.0)[:, 0] > 0).all()     # muted always wins


def test_dim_things_stay_visible_at_night():
    from tesseract.leds import to_pixels
    cfg, st, leds = _idle(muted=True)
    out = to_pixels(leds._scene(st, 1.0, 1.0), cfg.leds.night_brightness)
    assert (out[:, 0] >= 1).all()                            # the muted glow used to round to zero at night
    assert to_pixels(np.ones((1, 3)), cfg.leds.brightness).max() == round(cfg.leds.brightness * 255)
