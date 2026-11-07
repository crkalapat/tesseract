"""Whole-device tests with the cloud faked out, checking what you'd hear and the
states the LEDs/display would show.

  voice timer     WAV as the mic -> wake word -> speculative STT -> answered on the device (no LLM)
  weather         fast model -> get_weather -> second round -> speech
  background job  fast model -> start_task -> "On it." (or a variation) -> thinking agent asks a question ->
                  your answer -> calendar event -> result announced, LEDs green
"""

import shutil
import subprocess
import json
import threading
import time

import numpy as np
import pytest

from tesseract import config
from tesseract.assistant import HANDOFF
from tesseract.state import State
from tesseract.wakeword import FEATURE_MODELS, ensure_model_files

WAKE = {"sherpa": "Franklin", "openwakeword": "Hey Jarvis"}   # the default engine, and the trained-model one


def tone(seconds=0.4):
    n = int(24000 * seconds)
    return (np.sin(np.arange(n) * 2 * np.pi * 300 / 24000) * 8000).astype("<i2").tobytes()


def make_app(tmp_path, monkeypatch, wav=None, engine="sherpa"):
    from tesseract.app import Tesseract

    cfg = config.load()
    ensure_model_files(cfg.path("models"), [*FEATURE_MODELS, "hey_jarvis_v0.1.onnx"])
    cfg.system.state_dir = str(tmp_path)
    cfg.wake.engine = engine
    cfg.wake.model = "models/does-not-exist.onnx"     # openwakeword -> hey_jarvis fallback
    cfg.assistant.follow_up = False
    app = Tesseract(cfg, sim=True, wav_input=wav)
    tts = []

    def speech_stream(text, cancel=None):
        tts.append(text)
        audio = tone()
        for i in range(0, len(audio), 4801):   # odd chunk size: exercises byte carry-over
            yield audio[i:i + 4801]

    monkeypatch.setattr(app.cloud, "speech_stream", speech_stream)
    monkeypatch.setattr(app.cloud, "warm", lambda: None)
    monkeypatch.setattr(app.tts_cache, "warm", lambda *a: None)
    seen, last = [], {}
    orig_set = app.status.set

    def recording_set(**kw):   # every change of the fields the LEDs/display draw, in order
        orig_set(**kw)
        for key in ("state", "task_state", "tint"):
            v = getattr(app.status, key)
            if last.get(key, object()) != v:
                last[key] = v
                seen.append((key, v))
    monkeypatch.setattr(app.status, "set", recording_set)
    return app, tts, seen


def run(app):
    t = threading.Thread(target=app.run, daemon=True)
    t.start()
    return t


def wait_for(cond, timeout=15):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def states(seen, key="state"):
    return [v for k, v in seen if k == key]


@pytest.fixture(params=list(WAKE))
def engine(request):
    return request.param


@pytest.fixture
def speech_wav(tmp_path, engine):
    if not shutil.which("say"):
        pytest.skip("needs macOS `say`")
    p = tmp_path / "in.wav"
    subprocess.run(["say", "-v", "Samantha", "-o", str(p), "--data-format=LEI16@16000",
                    f"{WAKE[engine]} [[slnc 250]] set a timer for ten minutes for the pasta"], check=True)
    # pad with silence so the VAD sees the end of speech
    import wave
    with wave.open(str(p)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    # a MEMS mic delivers speech ~20 dB below full scale and mic_gain_db makes that up;
    # feeding full-scale TTS would clip after the gain, which no real mic does
    x = (x * 0.1).astype(np.int16)
    x = np.concatenate([np.zeros(16000, np.int16), x, np.zeros(16000 * 3, np.int16)])
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(x.tobytes())
    return str(p)


def test_voice_timer_on_device(tmp_path, engine, speech_wav, monkeypatch):
    app, tts, seen = make_app(tmp_path, monkeypatch, wav=speech_wav, engine=engine)
    calls = []

    def transcribe(pcm):
        calls.append(len(pcm) / 16000)
        return "Set a timer for ten minutes for the pasta."

    def no_llm(*a, **kw):
        raise AssertionError("a timer must not need the LLM")

    monkeypatch.setattr(app.cloud, "transcribe", transcribe)
    monkeypatch.setattr(app.cloud, "chat_stream", no_llm)
    runner = run(app)
    assert wait_for(lambda: State.SPEAKING in states(seen) and app.status.state == State.IDLE and tts, 25)
    app.stop()
    runner.join(timeout=5)
    order = states(seen)
    assert order.index(State.LISTENING) < order.index(State.SPEAKING), order
    assert State.TRANSCRIBING not in order            # the transcript was ready the moment it stopped listening
    assert len(calls) == 1 and 2.0 < calls[0] < 6.0   # one early STT call, on the command (not 3 s of silence)
    assert tts == ["Pasta timer set for 10 minutes."]
    assert [(i.label, i.total) for i in app.alarms.timers()] == [("pasta", 600)]
    assert app.status.timers and app.status.timers[0][0] == "pasta"
    assert (tmp_path / "sim.png").exists()


def test_weather_through_the_fast_model(tmp_path, monkeypatch):
    app, tts, seen = make_app(tmp_path, monkeypatch)
    calls = {}

    def chat_stream(messages, tools=None, cancel=None, llm=None, model=None, **kw):
        if messages[-1]["role"] == "user":
            calls["tools"] = [t["function"]["name"] for t in tools]
            yield "tool_calls", [{"id": "c1", "type": "function", "function": {
                "name": "get_weather", "arguments": '{"location": ""}'}}]
            yield "done", "tool_calls"
        else:
            calls["tool_result"] = messages[-1]["content"]
            for tok in ["Tomorrow looks", " sunny,", " with a high of seventy five.", " Enjoy!"]:
                time.sleep(0.02)
                yield "text", tok
            yield "done", "stop"

    monkeypatch.setattr(app.cloud, "chat_stream", chat_stream)
    monkeypatch.setattr(app.weather, "report", lambda location="", days=3: "Austin now: 70°F. Tomorrow: sunny, high 75°F.")
    runner = run(app)
    assert wait_for(lambda: app.status.state == State.IDLE)
    app.events.put(("text", "What's the weather tomorrow?"))
    assert wait_for(lambda: State.SPEAKING in states(seen) and app.status.state == State.IDLE and len(tts) >= 2)
    app.stop()
    runner.join(timeout=5)
    order = states(seen)
    for expected in (State.THINKING, State.WORKING, State.SPEAKING):
        assert expected in order, expected
    assert {"get_weather", "web_search", "start_task", "set_timer"} <= set(calls["tools"])
    assert "sunny" in calls["tool_result"]
    assert tts == ["Tomorrow looks sunny, with a high of seventy five.", "Enjoy!"]


def test_background_job_asks_then_reports(tmp_path, monkeypatch):
    app, tts, seen = make_app(tmp_path, monkeypatch)

    def chat_stream(messages, tools=None, cancel=None, llm=None, model=None, **kw):
        assert messages[-1]["role"] == "user"
        yield "tool_calls", [{"id": "c1", "type": "function", "function": {
            "name": "start_task", "arguments": '{"request": "Add the robotics club meeting this Friday to my calendar"}'}}]
        yield "done", "tool_calls"

    steps = []

    def complete(messages, tools=None, llm=None, model=None, **kw):
        steps.append(messages[-1])
        assert llm is app.cfg.thinking
        if len(steps) == 1:
            time.sleep(0.3)
            return {"content": None, "tool_calls": [{"id": "t1", "type": "function", "function": {
                "name": "ask_user", "arguments": '{"question": "What time on Friday?"}'}}]}
        if len(steps) == 2:
            return {"content": None, "tool_calls": [{"id": "t2", "type": "function", "function": {
                "name": "calendar_add", "arguments": '{"title": "Robotics club", "start": "2026-10-23T18:00"}'}}]}
        return {"content": "Done. Robotics club is on your calendar for Friday at six."}

    monkeypatch.setattr(app.cloud, "chat_stream", chat_stream)
    monkeypatch.setattr(app.cloud, "complete", complete)
    runner = run(app)
    assert wait_for(lambda: app.status.state == State.IDLE)
    app.events.put(("text", "Add the robotics club meeting this Friday to my calendar"))
    # "On it." (or a variation), then the job's question is read out and it waits for you
    assert wait_for(lambda: app.agents.waiting() and "What time on Friday?" in tts and app.status.state == State.IDLE)
    assert tts[0] in HANDOFF
    assert app.status.task_state == "waiting"
    task = app.agents.waiting()[0]
    app._start_turn(text="Six pm.", answer_for=task)  # what the follow-up listening turn does with your answer
    assert wait_for(lambda: task.done.is_set() and tts[-1].startswith("Done.") and app.status.state == State.IDLE)
    app.stop()
    runner.join(timeout=5)
    assert steps[1] == {"role": "tool", "tool_call_id": "t1", "name": "ask_user", "content": "Six pm."}
    assert [e["title"] for e in app.organizer.data["events"]] == ["Robotics club"]
    assert tts[1:] == ["What time on Friday?", "Got it.", "Done. Robotics club is on your calendar for Friday at six."]
    task_states = states(seen, "task_state")
    assert task_states[:4] == ["", "running", "waiting", "running"], task_states
    assert task_states[-1] == ""                              # read out: no green "done" flash (green = quiet mode)
    assert ["waiting"] == [t for t in states(seen, "tint") if t]           # only the question is tinted (amber)
    assert app.tts_cache.get(tts[0]) is not None              # kept, so next time it's instant


def test_button_interrupts_speech(tmp_path, monkeypatch):
    app, tts, seen = make_app(tmp_path, monkeypatch)

    def chat_stream(messages, tools=None, cancel=None, llm=None, model=None, **kw):
        yield "text", "Once upon a time there was a very long story. It went on and on."
        yield "done", "stop"

    def speech_stream(text, cancel=None):
        audio = tone(5)
        for i in range(0, len(audio), 4800):
            if cancel is not None and cancel.is_set():
                return
            time.sleep(0.01)
            yield audio[i:i + 4800]

    monkeypatch.setattr(app.cloud, "chat_stream", chat_stream)
    monkeypatch.setattr(app.cloud, "speech_stream", speech_stream)
    runner = run(app)
    assert wait_for(lambda: app.status.state == State.IDLE)
    app.events.put(("text", "tell me a story"))
    assert wait_for(lambda: app.status.state == State.SPEAKING, 10)
    time.sleep(0.5)
    app.events.put(("action",))
    t0 = time.monotonic()
    while (app.status.state != State.IDLE or app.mixer.busy) and time.monotonic() - t0 < 2:
        time.sleep(0.01)
    stopped_after = time.monotonic() - t0
    app.stop()
    runner.join(timeout=5)
    assert app.status.state == State.IDLE
    assert not app.mixer.busy
    assert stopped_after < 0.5, stopped_after


def test_ignores_its_own_name(tmp_path, monkeypatch):
    """"Okay, back to Franklin." comes out of the speaker and into the mic; that must not wake it."""
    from tesseract.app import Tesseract
    from tesseract.speech import SpeechSession

    cfg = config.load()
    cfg.system.state_dir = str(tmp_path)
    app = Tesseract(cfg, sim=True)
    started = []
    monkeypatch.setattr(app, "_start_turn", lambda **kw: started.append(kw))

    class Cloud:
        def speech_stream(self, text, cancel=None):
            return iter(())

    class Mixer:
        def play(self, source):
            return source

    app.speech = SpeechSession(Cloud(), Mixer(), cfg, threading.Event())
    app.speech.say(f"Okay, back to {cfg.assistant.name}.")
    app.status.set(state=State.SPEAKING)
    app._handle("wake", 1.0)
    assert started == []                    # still playing
    app.speech.finished_at = time.monotonic() - 0.5
    app._handle("wake", 1.0)
    assert started == []                    # just finished; the detector fires a moment late
    app.speech.finished_at = time.monotonic() - 5
    app._handle("wake", 1.0)
    assert len(started) == 1                # that one was really you


def test_quiet_mode_holds_results_until_asked(tmp_path, monkeypatch):
    app, tts, seen = make_app(tmp_path, monkeypatch)

    def chat_stream(messages, tools=None, cancel=None, llm=None, model=None, **kw):
        yield "tool_calls", [{"id": "c1", "type": "function", "function": {
            "name": "start_task", "arguments": '{"request": "What is on my to-do list?"}'}}]
        yield "done", "tool_calls"

    monkeypatch.setattr(app.cloud, "chat_stream", chat_stream)
    monkeypatch.setattr(app.cloud, "complete", lambda *a, **kw: {"content": "Two things: laundry and physics."})
    runner = run(app)
    assert wait_for(lambda: app.status.state == State.IDLE)
    app.events.put(("text", "Turn on quiet mode"))
    assert wait_for(lambda: app.status.quiet and app.status.state == State.IDLE)
    app.events.put(("text", "What's on my to-do list"))
    assert wait_for(lambda: app.held and app.status.task_state == "ready" and app.status.state == State.IDLE)
    time.sleep(0.3)
    assert not any("laundry" in t for t in tts)                      # finished, but not blurted out
    app.events.put(("action",))                                       # a tap reads it
    assert wait_for(lambda: any("laundry" in t for t in tts) and app.status.state == State.IDLE)
    assert not app.held
    app.events.put(("text", "Quiet mode off"))
    assert wait_for(lambda: not app.status.quiet)
    app.stop()
    runner.join(timeout=5)
    assert json.loads((tmp_path / "settings.json").read_text())["quiet"] is False
