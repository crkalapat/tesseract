"""The latency work: on-device commands, speculative endpointing, the TTS cache,
the mixer's shallow silence, and the Open-Meteo weather path."""

import datetime as dt
import queue
import threading
import time

import numpy as np
import pytest

from tesseract import config
from tesseract.alarms import Alarms
from tesseract.app import normalize
from tesseract.commands import LocalCommands, parse_duration
from tesseract.vad import looks_complete, record_utterance


class Hooks:
    def __init__(self):
        self.vol = 5

    def set_volume(self, v):
        self.vol = max(0, min(10, v))

    def volume(self):
        return self.vol


@pytest.fixture
def local(tmp_path):
    cfg = config.load()
    return LocalCommands(cfg, Alarms(tmp_path / "a.json", on_fire=print), Hooks())


@pytest.mark.parametrize("text,seconds", [
    ("ten minutes", 600), ("10 minutes", 600), ("10min", 600), ("an hour and a half", 5400),
    ("2 and a half minutes", 150), ("1 hour 30 minutes", 5400), ("half an hour", 1800),
    ("twenty five minutes", 1500), ("90 seconds", 90), ("a minute", 60), ("1.5 hours", 5400)])
def test_parse_duration(text, seconds):
    from tesseract.commands import _words
    assert parse_duration(_words(text))[0] == seconds


@pytest.mark.parametrize("said,reply,label", [
    ("Set a timer for ten minutes.", "Timer set for 10 minutes.", ""),
    ("Franklin, set a pasta timer for 10 minutes", "Pasta timer set for 10 minutes.", "pasta"),
    ("Set a timer for 5 minutes for the eggs, please.", "Eggs timer set for 5 minutes.", "eggs"),
    ("Start a 90 second timer", "Timer set for 1 minute and 30 seconds.", ""),
    ("Timer, 20 minutes.", "Timer set for 20 minutes.", ""),
])
def test_timer_on_device(local, said, reply, label):
    r = local.handle(normalize(said, "Franklin"))
    assert r.text == reply and r.cache
    assert [t.label for t in local.alarms.timers()] == [label]


@pytest.mark.parametrize("said", [
    "Set a timer.",                                                 # how long? the model asks
    "Set a timer for 10 minutes and remind me to flip the steak",   # more than a timer
    "What's a good timer app?", "Set an alarm for 7 tomorrow", "How long is a marathon?",
    "Add 5 minutes to the timer"])
def test_not_for_the_device(local, said):
    assert local.handle(normalize(said, "Franklin")) is None


def test_time_left_and_cancel(local):
    assert local.handle("how much time is left").text == "You don't have a timer running."
    local.handle("set a pasta timer for 10 minutes")
    local.handle("set an egg timer for 3 minutes")
    left = local.handle("how much time is left on the pasta timer").text
    assert left.startswith("9 minutes and 5") or left.startswith("10 minutes"), left
    assert local.handle("cancel the timer") is None                 # two running, none named: ask
    assert local.handle("cancel the egg timer").text == "Egg timer cancelled."
    assert local.handle("cancel the timer").text == "Pasta timer cancelled."
    assert local.handle("stop the timer").text == "Okay."            # e.g. right after it rang


def test_volume_and_clock(local):
    assert local.handle("turn it up").text == "" and local.hooks.vol == 7
    assert local.handle("set the volume to 3").text == "" and local.hooks.vol == 3
    assert local.handle("volume 80 percent").text == "" and local.hooks.vol == 8
    assert local.handle("what time is it").text.startswith("It's ")
    assert local.handle("whats the date").text.startswith(f"It's {dt.date.today():%A}")


@pytest.mark.parametrize("text,done", [
    ("What's the weather tomorrow?", True), ("Set a timer for ten minutes.", True),
    ("Set a timer for.", False), ("Add milk and.", False), ("What's the...", False), ("", False),
    ("um", False), ("I need to talk to", False)])
def test_looks_complete(text, done):
    assert looks_complete(text) is done


class FakeMic:
    """Feeds 20 ms frames of 'speech' (loud tone) and silence at real-time pace."""

    def __init__(self, pattern):   # [(seconds, is_speech), ...]
        self.pattern = pattern
        self.q = queue.Queue()

    def subscribe(self):
        def go():
            for secs, speech in self.pattern:
                for _ in range(int(secs * 50)):
                    t = np.arange(320) / 16000
                    x = (np.sin(2 * np.pi * 220 * t) * 6000 * (1 + 0.5 * np.sin(2 * np.pi * 5 * t))) if speech \
                        else np.random.default_rng().normal(0, 20, 320)
                    self.q.put(x.astype(np.int16))
                    time.sleep(0.02)
        threading.Thread(target=go, daemon=True).start()
        return self.q

    def unsubscribe(self, q):
        pass


def _record(pattern, reply, monkeypatch=None):
    cfg = config.load()
    calls = []

    def transcribe(pcm):
        calls.append(len(pcm) / 16000)
        time.sleep(0.15)                  # a quick STT round trip
        return reply(len(calls))

    import tesseract.vad as vad
    det = vad.SpeechDetector(0)
    det.vad = None                        # webrtcvad doesn't take a tone for a voice; use the energy gate
    orig = vad.speech_detector
    vad.speech_detector = lambda cfg: det
    try:
        utt = record_utterance(cfg, FakeMic(pattern), threading.Event(), transcribe=transcribe)
        return utt, calls, time.monotonic() - utt.ended_at   # how long it took to decide you were done
    finally:
        vad.speech_detector = orig


def test_endpoint_early_on_a_finished_sentence():
    utt, calls, waited = _record([(0.2, False), (1.0, True), (2.0, False)], lambda n: "What's the weather?")
    assert utt.text == "What's the weather?" and len(calls) == 1
    # the transcript is ready long before; it still gives you the full 750 ms pause to go on,
    # but no more (not the 1.2 s for a sentence that trails off)
    assert 0.74 <= waited < 0.75 + 0.15, waited


def test_endpoint_waits_when_it_trails_off():
    utt, calls, waited = _record([(0.2, False), (1.0, True), (2.0, False)], lambda n: "Set a timer for.")
    assert utt.text == "Set a timer for." and len(calls) == 1
    assert waited >= 0.75, waited                        # gave you the full end_silence_ms to go on


def test_endpoint_discards_the_guess_if_you_keep_talking():
    utt, calls, _ = _record([(0.2, False), (0.8, True), (0.45, False), (0.8, True), (1.5, False)],
                            lambda n: ["Set a timer for.", "Set a timer for ten minutes."][min(n, 2) - 1])
    assert len(calls) == 2                               # a second guess after the second pause
    assert utt.text == "Set a timer for ten minutes."


def test_tts_cache(tmp_path):
    from tesseract.speech import TTSCache
    cfg = config.load()
    cfg.system.state_dir = str(tmp_path)
    c = TTSCache(cfg, max_files=2)
    assert c.get("On it!") is None
    c.put("On it!", np.full(4800, 0.5, np.float32))
    assert np.allclose(c.get("On it!"), 0.5, atol=1e-3)
    cfg.tts.voice += "-another-voice"
    assert c.get("On it!") is None                       # another voice never plays the old audio
    c.put("a", np.zeros(10, np.float32))
    time.sleep(0.01)
    c.put("b", np.zeros(10, np.float32))
    assert len(list(c.dir.glob("*.npy"))) == 2           # capped


def test_mixer_keeps_little_silence_queued():
    """While a reply is still being synthesized, at most ~IDLE_LEAD of silence may sit
    ahead of it in the sound card; otherwise the first word waits behind all of it."""
    from tesseract.playback import BLOCK, IDLE_LEAD, Mixer, StreamSource

    writes = []

    class Out:
        def open(self):
            self.t = time.monotonic()

        def write(self, x):
            writes.append((time.monotonic(), float(np.abs(x).max())))
            # a sound card with a 128 ms buffer: accepts until full, then paces
            self.t = max(self.t, time.monotonic()) + len(x) / 48000
            time.sleep(max(0.0, self.t - time.monotonic() - 0.128))

        def close(self, drain=True):
            pass

    class Audio:
        def output(self):
            return Out()

    mixer = Mixer(Audio(), lambda: 1.0)
    mixer.start()
    src = mixer.play(StreamSource())
    time.sleep(0.4)                                      # "TTS still downloading"
    src.push(np.full(BLOCK * 5, 0.5, np.float32))
    src.close()
    t_audio = time.monotonic()
    assert src.done.wait(2)
    first = next(i for i, (_, peak) in enumerate(writes) if peak > 0)
    queued_ahead = first * BLOCK / 48000 - (writes[first][0] - writes[0][0])
    assert queued_ahead <= IDLE_LEAD + BLOCK / 48000 + 0.01, queued_ahead
    assert writes[first][0] - t_audio < 0.05


def test_weather_report_format():
    from tesseract.weather import format_report
    today = dt.date.today()
    j = {"current": {"temperature_2m": 71.6, "apparent_temperature": 70.2, "weather_code": 2, "wind_speed_10m": 8.4},
         "daily": {"time": [str(today), str(today + dt.timedelta(days=1))], "weather_code": [2, 61],
                   "temperature_2m_max": [80.1, 75.0], "temperature_2m_min": [61.0, 58.9],
                   "precipitation_probability_max": [10, 70]}}
    assert format_report(j, "Austin, Texas", "°F", "mph") == (
        "Austin, Texas now: 72°F (feels 70°F), partly cloudy, wind 8 mph. "
        "Today: partly cloudy, high 80°F, low 61°F, 10% chance of rain. "
        "Tomorrow: light rain, high 75°F, low 59°F, 70% chance of rain.")


def test_weather_prefers_the_named_region(monkeypatch):
    from tesseract.weather import Weather

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"name": "Springfield", "admin1": "Missouri", "country": "United States",
                                 "latitude": 37.2, "longitude": -93.3},
                                {"name": "Springfield", "admin1": "Illinois", "country": "United States",
                                 "latitude": 39.8, "longitude": -89.6}]}

    w = Weather(config.load())
    monkeypatch.setattr(w.client, "get", lambda *a, **k: R())
    assert w.place("Springfield, Illinois") == (39.8, -89.6, "Springfield, Illinois")


def test_organizer(tmp_path):
    from tesseract.organizer import Organizer
    o = Organizer(tmp_path / "o.json")
    o.todo_add("buy milk", "2026-10-21")
    o.todo_add("call mom")
    assert [t["text"] for t in o.todos()] == ["buy milk", "call mom"]
    o.todo_update("milk", "complete")
    assert [t["text"] for t in o.todos()] == ["call mom"]
    ev = o.event_add("Robotics club", "2026-10-23 18:00", location="Engineering 101")
    assert not ev["all_day"] and ev["start"] == "2026-10-23T18:00"
    assert o.events_between(dt.datetime(2026, 10, 23), dt.datetime(2026, 10, 24))[0]["title"] == "Robotics club"
    assert Organizer(tmp_path / "o.json").data["events"][0]["location"] == "Engineering 101"   # persisted


def test_mixer_never_lets_the_card_run_dry():
    """Between sounds the card keeps getting silence (a card that runs dry replays stale buffer
    contents: the "zit zit"), and closing ends on silence before it stops."""
    from tesseract.playback import BLOCK, ArraySource, Mixer

    writes, closed = [], []

    class Out:
        def open(self):
            self.t = time.monotonic()

        def write(self, x):
            writes.append((time.monotonic(), float(np.abs(x).max())))
            self.t = max(self.t, time.monotonic()) + len(x) / 48000
            time.sleep(max(0.0, self.t - time.monotonic() - 0.128))

        def close(self, drain=True):
            closed.append(drain)

    class Audio:
        def output(self):
            return Out()

    mixer = Mixer(Audio(), lambda: 1.0)
    mixer.start()
    time.sleep(0.2)
    assert writes, "opened right away"
    mixer.play(ArraySource(np.full(BLOCK * 3, 0.5, np.float32)))
    time.sleep(0.6)                                       # the sound ends; the card keeps running
    t_quiet = [t for t, peak in writes if peak == 0 and t > writes[-1][0] - 0.4]
    gaps = np.diff([t for t, _ in writes if t > writes[-1][0] - 0.4])
    assert t_quiet and gaps.max() < 0.05, gaps.max()      # fed every ~20 ms, never left to run dry
    mixer.close()
    assert closed == [True] and all(peak == 0 for _, peak in writes[-5:])


@pytest.mark.parametrize("raw,clean", [
    ("*phone rings*  Tell me a joke.", "Tell me a joke."), (".  What time is it?", "What time is it?"),
    ("[Music] Set a timer (laughs) for one minute.", "Set a timer for one minute."), ("", "")])
def test_clean_transcript(raw, clean):
    from tesseract.cloud import clean_transcript
    assert clean_transcript(raw) == clean


def test_stuck_early_transcription_is_raced():
    """The guess that hung for 10+ s on the Pi: now a fresh transcription takes over after ~1 s."""
    import tesseract.vad as vad
    cfg = config.load()
    calls = []

    def transcribe(pcm):
        calls.append(time.monotonic())
        if len(calls) == 1:
            time.sleep(30)                # stuck
        return "Set a timer for ten minutes."

    det = vad.SpeechDetector(0)
    det.vad = None
    orig = vad.speech_detector
    vad.speech_detector = lambda cfg: det
    try:
        utt = record_utterance(cfg, FakeMic([(0.2, False), (1.0, True), (2.0, False)]), threading.Event(),
                               transcribe=transcribe)
    finally:
        vad.speech_detector = orig
    waited = time.monotonic() - utt.ended_at
    assert utt.text == "Set a timer for ten minutes." and len(calls) == 2
    assert waited < cfg.listen.end_silence_ms / 1000 + vad.GUESS_GRACE_S + 0.5, waited   # not 10 s


def test_mute_and_quiet_mode_by_voice():
    from tesseract.app import normalize
    from tesseract.commands import QUIET_OFF, QUIET_ON, SOUND_OFF, SOUND_ON, LocalCommands

    class H:
        vol, mic, quiet = 6, False, False
        def set_volume(self, v): self.vol = v
        def volume(self): return self.vol
        def mute_mic(self): self.mic = True
        def mute_sound(self, on):
            if on:
                self.vol = 0
                return True
            if self.vol:
                return False
            self.vol = 6
            return True
        def set_quiet(self, on): self.quiet = on

    h = H()
    c = LocalCommands(config.load(), None, h)
    for s in ["Mute", "mute yourself", "Stop listening", "turn off your mic", "mute the microphone please"]:
        r = c.handle(normalize(s, "franklin"))
        assert r and r.text == "" and r.then, s          # just does it, no words
    r.then()
    assert h.mic
    r = c.handle(normalize("Mute the sound", "franklin"))
    assert r.text == SOUND_OFF and h.vol == 6
    r.then()
    assert h.vol == 0
    assert c.handle(normalize("unmute", "franklin")).text == SOUND_ON and h.vol == 6
    assert c.handle(normalize("unmute", "franklin")).text == "I'm not muted."
    for s, want in [("Turn on quiet mode", QUIET_ON), ("enable quiet mode", QUIET_ON), ("quiet mode", QUIET_ON),
                    ("turn off quiet mode", QUIET_OFF), ("quiet mode off", QUIET_OFF)]:
        r = c.handle(normalize(s, "franklin"))
        assert r and r.text == want, s
    assert c.handle(normalize("be quiet", "franklin")) is None            # that's "stop talking", handled before


def test_asking_for_held_results():
    from tesseract.app import READ_HELD, normalize
    for s in ["What did you find?", "Read it", "Read it out to me", "What's the answer?", "What have you got for me",
              "What did Hermes say", "Go ahead", "any news"]:
        assert READ_HELD.fullmatch(normalize(s, "franklin")), s
    assert not READ_HELD.fullmatch(normalize("Read my email", "franklin"))
