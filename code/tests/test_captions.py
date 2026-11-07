"""Closed captions: the screen shows the sentence being heard, when it's heard."""

import datetime as dt
import time

import numpy as np

from tesseract import config
from tesseract.playback import StreamSource
from tesseract.state import State, Status


def test_caption_changes_when_playback_reaches_the_sentence():
    src = StreamSource()
    seen = []
    src.on_mark = seen.append
    src.mark("First sentence.")
    src.push(np.ones(48000, np.float32))
    src.mark("Second one.")
    src.push(np.ones(48000, np.float32))
    src.close()
    src.read(1000)
    assert seen == ["First sentence."]                  # the first one starts playing
    src.read(46000)
    assert seen == ["First sentence."]                  # still inside it
    src.read(2000)
    assert seen == ["First sentence.", "Second one."]   # crossed into the second


def test_speaking_screen_is_just_the_caption():
    from PIL import ImageDraw
    from tesseract.display import Display
    cfg = config.load()
    s = Status()
    disp = Display(cfg, s)
    s.set(state=State.SPEAKING, reply_text="It's sunny. High of seventy five.", caption="It's sunny.",
          caption_at=time.monotonic())
    drawn = []
    real_text = ImageDraw.ImageDraw.text
    def spy(self, xy, text, *a, **kw):
        drawn.append(text)
        return real_text(self, xy, text, *a, **kw)
    ImageDraw.ImageDraw.text = spy
    try:
        disp.render(s, dt.datetime(2026, 11, 5, 9, 0))
    finally:
        ImageDraw.ImageDraw.text = real_text
    assert "It's sunny." in drawn
    assert "Speaking" not in drawn and "High of seventy five." not in drawn   # not ahead of the voice
