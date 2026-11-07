import datetime as dt

import numpy as np
import pytest

from tesseract import config, dsp
from tesseract.__main__ import check_pins
from tesseract.alarms import Alarms, next_occurrence
from tesseract.app import STOP_PHRASES, normalize
from tesseract.speech import SentenceSplitter, clean_for_speech


def test_resampler_streaming_matches_tone():
    for src, dst in ((48000, 16000), (24000, 48000), (22050, 48000)):
        f = 440.0
        x = np.sin(2 * np.pi * f * np.arange(src) / src).astype(np.float32)
        rs = dsp.StreamResampler(src, dst)
        # odd chunk sizes to exercise state carry-over
        out = np.concatenate([rs.process(c) for c in np.array_split(x, 37)])
        assert abs(len(out) - dst) <= 2, (src, dst, len(out))
        ref = np.sin(2 * np.pi * f * np.arange(len(out)) / dst)
        # skip the filter's warm-up; then it should be the same tone (allowing group delay)
        seg, refseg = out[2000:6000], ref
        best = max(np.corrcoef(seg, refseg[2000 - d:6000 - d])[0, 1] for d in range(0, 60))
        assert best > 0.999, (src, dst, best)


def test_decimator_rejects_alias():
    # 10 kHz is above 16 kHz's Nyquist; it must not fold back into the speech band
    x = np.sin(2 * np.pi * 10000 * np.arange(48000) / 48000).astype(np.float32)
    y = dsp.StreamResampler(48000, 16000).process(x)
    assert dsp.rms(y[1000:]) < 0.01


def test_volume_curve():
    assert dsp.volume_to_gain(0) == 0
    assert dsp.volume_to_gain(10) == pytest.approx(1.0)
    gains = [dsp.volume_to_gain(v) for v in range(1, 11)]
    assert gains == sorted(gains)


def test_sentence_splitter():
    sp = SentenceSplitter()
    out = []
    for tok in ["Sure", ". Dr", ". Smith arrives at 3", ".5 p", ".m. tomorrow", "! Anything", " else?"]:
        out += sp.feed(tok)
    out += sp.flush()
    assert out == ["Sure.", "Dr. Smith arrives at 3.5 p.m. tomorrow!", "Anything else?"]


def test_splitter_first_clause_early():
    sp = SentenceSplitter(first_clause_chars=60)
    out = sp.feed("The forecast for tomorrow in Austin looks great overall, with sunshine all day and ")
    assert out == ["The forecast for tomorrow in Austin looks great overall,"]


def test_clean_for_speech():
    assert clean_for_speech("**Bold** and [a link](http://x.com) 🎉\n- item") == "Bold and a link. item"


@pytest.mark.parametrize("raw,spoken", [
    ("Sunny, high of 75 ([weather.gov](https://www.weather.gov/fwd?x=1)).", "Sunny, high of 75."),   # citations go
    ("Renew it at [the DMV site](https://www.txdmv.gov/renew) or in person.", "Renew it at the DMV site or in person."),
    ("See [here](https://docs.python.org/3/library/re.html) for more.", "See the Python website for more."),
    ("Renew it at https://www.txdmv.gov/renew today.", "Renew it at the TXDMV website today."),
    ("Renew it at dmv.org or in person.", "Renew it at the DMV website or in person."),
    # abbreviations a voice reads wrong are spelled out
    ("The game is on Fri, Oct 31 at 7 PM.", "The game is on Friday, October 31 at 7 PM."),
    ("Winds of 15 mph and highs near 75°F.", "Winds of 15 miles per hour and highs near 75 degrees Fahrenheit."),
    ("It takes 1 min, about 20 mins total.", "It takes 1 minute, about 20 minutes total."),
    ("Bring snacks, drinks, etc. See you there.", "Bring snacks, drinks, and so on. See you there."),
    ("Lakers vs. Celtics, e.g. tonight.", "Lakers versus Celtics, for example, tonight."),
    ("The meeting moved to Dec. 12.", "The meeting moved to December 12."),
    ("I saw the sun, then it set.", "I saw the sun, then it set."),
    # what Haiku tacks on after web searches: none of it is read out
    ("It will rain. [1](https://weather.gov/x) [2](https://accuweather.com/y)", "It will rain."),
    ("It will rain. [weather.gov](https://weather.gov/x)", "It will rain."),
    ("It will rain. https://weather.gov/x https://www.accuweather.com/y", "It will rain."),
    ("It will rain tomorrow (weather.gov, accuweather.com).", "It will rain tomorrow."),
    ("It will rain. Sources: weather.gov, nws.noaa.gov", "It will rain."),
    ("The Rockets won.\n\n[ESPN](https://espn.com/nba) | [NBA.com](https://nba.com/x)", "The Rockets won."),
    ("According to [ESPN](https://espn.com/x), the Rockets won.", "According to ESPN, the Rockets won."),
    ("It costs 3.50 dollars, about 2.5 percent more.", "It costs 3.50 dollars, about 2.5 percent more."),
    ("Found three:\n1. **Eggs**\n2. Milk\n3. Bread", "Found three: Eggs. Milk. Bread"),
    ("Results [1][2] show it's up 3%.", "Results show it's up 3 percent."),
    ("It's done.\n\nSources: [AP](https://apnews.com/x), [Reuters](https://reuters.com/y)", "It's done."),
])
def test_links_are_readable_out_loud(raw, spoken):
    assert clean_for_speech(raw) == spoken


def test_stop_phrases():
    assert normalize("Franklin, never mind.", "Franklin") in STOP_PHRASES
    assert normalize("Hey Franklin stop!", "Franklin") in STOP_PHRASES
    assert normalize("Franklin what time is it", "Franklin") not in STOP_PHRASES


def test_alarm_next_occurrence():
    fri = dt.datetime(2026, 10, 2, 8, 0)  # a Friday
    assert next_occurrence("07:00", "weekdays", fri) == dt.datetime(2026, 10, 5, 7, 0)
    assert next_occurrence("09:00", "weekdays", fri) == dt.datetime(2026, 10, 2, 9, 0)
    assert next_occurrence("07:00", "weekends", fri) == dt.datetime(2026, 10, 3, 7, 0)


def test_alarms_fire_and_cancel(tmp_path):
    fired = []
    a = Alarms(tmp_path / "a.json", on_fire=fired.append)
    a.add_timer(0.2, "pasta")
    a.add_alarm("07:00", "wake up", "daily")
    assert len(a.list()) == 2
    a.start()
    import time
    time.sleep(1.2)
    assert [f.label for f in fired] == ["pasta"]
    assert a.next_alarm_text() in ("7:00 AM",) or "7:00 AM" in a.next_alarm_text()
    assert len(a.cancel("alarms")) == 1 and not a.list()
    # persisted
    assert Alarms(tmp_path / "a.json", on_fire=print).list() == []


def test_default_pins_have_no_errors():
    cfg = config.load()
    errors = [p for p in check_pins(cfg) if not p.startswith("note")]
    assert errors == []


def test_pin_checker_catches_original_led_pin():
    cfg = config.load()
    cfg.pins.led_data = 26
    assert any("PWM" in p for p in check_pins(cfg))
    cfg.pins.led_data = 13
    cfg.pins.button_mute = 20
    assert any("I2S" in p for p in check_pins(cfg))


def test_rgb565_packing():
    from PIL import Image

    from tesseract.display import SSD1333

    class FakeSpi:
        def __init__(self):
            self.data = b""

        def writebytes(self, b):
            pass

        def writebytes2(self, b):
            self.data = bytes(b)

    class Pin:
        def on(self): pass
        def off(self): pass

    d = SSD1333.__new__(SSD1333)
    d.spi, d.dc, d.bgr, d._last = FakeSpi(), Pin(), False, None
    img = Image.new("RGB", (176, 176), (255, 0, 0))
    d.show(img)
    assert d.spi.data[:2] == b"\xf8\x00"            # pure red, big-endian RGB565
    img.putpixel((5, 7), (0, 0, 255))
    d.show(img)
    assert d.spi.data == b"\x00\x1f"                # only the one changed pixel was sent
