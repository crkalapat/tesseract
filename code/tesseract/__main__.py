"""python -m tesseract [command]

  run            (default) the smart speaker
  doctor         check pins, sound card, API key, agents
  say TEXT       speak TEXT through the speaker (tests TTS + amp)
  ask TEXT       full answer pipeline without the mic (tests LLM + TTS)
  record [SECS]  record from the mic, show the level, play it back
  wake           live wake-word scores (for tuning the threshold)
  buttons        print each button press (wiring check)
  setup          set the location and add Wi-Fi networks, step by step
  location PLACE where Tesseract is (weather, local answers, the clock's time zone)
  voices [MODEL] hear the same sentence in several voices, to pick one for [tts]
  brightness [0-10|up|down|night|morning]  the screen's brightness, or night mode on/off
  perf           where the CPU goes, and whether the sound card keeps up (stop the service first)
  wifi           saved and nearby networks
    wifi add NAME [PASSWORD] [--user U]   save a network for later (--user: eduroam-style Wi-Fi)
    wifi forget NAME                      remove one
    wifi setup                            open the phone setup hotspot now
  speaker [VOL]  crackle check: test tones, dropouts and the power supply (stop the service first)
  interrupt-test can it hear "Franklin" over its own voice? (stop the service first)
  spotify setup  connect Spotify (Premium): play, pause, next... by voice
    spotify | spotify off                 what's connected | turn it off
  routing-test [MODEL ...]  how often the fast model picks the right tool, and how fast (compare models)
  hermes URL     connect your Hermes Agent (asks for its API key, tests it)
    hermes off                            disconnect it
  local URL [MODEL]  use a model on your computer (LM Studio, Ollama) for answers
    local on | off                        switch between it and the cloud
  leds           cycle the LED animations
  display        cycle the screens
Add --sim to run on a laptop (laptop mic/speaker, screen -> data/sim.png).
"""

from __future__ import annotations

import argparse
import os
import logging
import sys
import threading
import time
from pathlib import Path

from . import config as config_mod


def check_pins(cfg) -> list[str]:
    """Human-readable problems with the [pins] section."""
    p = cfg.pins
    problems = []
    used = {
        "button_action": p.button_action, "button_vol_up": p.button_vol_up,
        "button_vol_down": p.button_vol_down, "button_mute": p.button_mute, "led_data": p.led_data,
        "display_dc": p.display_dc, "display_reset": p.display_reset, "display_cs": p.display_cs,
        "amp_enable_dummy" if not p.amp_enable else "amp_enable": p.amp_enable or p.amp_enable_dummy,
    }
    fixed = {18: "I2S BCLK", 19: "I2S LRCLK", 20: "I2S mic data", 21: "I2S amp data",
             10: "SPI0 MOSI (display)", 11: "SPI0 SCLK (display)", 9: "SPI0 MISO (claimed by SPI)"}
    seen: dict[int, str] = {}
    for name, pin in used.items():
        if not 0 <= pin <= 27:
            problems.append(f"{name}=GPIO{pin}: not a header GPIO (0-27)")
        if pin in fixed:
            problems.append(f"{name}=GPIO{pin}: already used by {fixed[pin]}")
        if pin in seen:
            problems.append(f"{name}=GPIO{pin}: same pin as {seen[pin]}")
        seen[pin] = name
    if p.led_data not in (12, 13, 18, 19):
        problems.append(f"led_data=GPIO{p.led_data}: WS2812s need a PWM pin (12, 13, 18, 19). "
                        "Use GPIO12 (header pin 32) or GPIO13 (header pin 33).")
    for name, pin in used.items():
        if pin in (0, 1) and name != "amp_enable_dummy":
            problems.append(f"note: {name}=GPIO{pin} is a HAT-EEPROM ID pin; add force_eeprom_read=0 "
                            "to config.txt, or use a free pin like GPIO 23/24.")
        if pin == 7 and name != "display_cs":
            problems.append(f"note: {name}=GPIO7 needs dtoverlay=spi0-1cs (install.sh sets it) or SPI claims it as CE1.")
    if p.display_cs != 8:
        problems.append(f"note: display chip select on GPIO{p.display_cs} instead of GPIO8 (install.sh sets "
                        "dtoverlay=spi0-1cs,cs0_pin=...; re-run it after changing display_cs)")
        if pin in (14, 15):
            problems.append(f"note: {name}=GPIO{pin} is the UART; disable the serial console in raspi-config.")
        if pin in (2, 3):
            problems.append(f"note: {name}=GPIO{pin} has a 1.8k pull-up on the Pi and is the I2C bus.")
    return problems


def cmd_doctor(cfg) -> int:
    from pathlib import Path
    ok = True
    print("── pins")
    probs = check_pins(cfg)
    for pr in probs:
        print(("  ! " if not pr.startswith("note") else "  · ") + pr)
        ok &= pr.startswith("note")
    if not probs:
        print("  ok")

    th = _throttled()
    if th is not None:
        print("── power")
        if th & 0x1:
            print(f"  UNDER-VOLTAGE right now (0x{th:x}): use a 5 V 3 A supply; a weak one makes the speaker crackle")
            ok = False
        elif th & 0x10000:
            print(f"  under-voltage happened since boot (0x{th:x}): the supply sags under load (speaker pops?)")
        else:
            print("  ok")
    print("── boot config")
    cfgtxt = next((p for p in (Path("/boot/firmware/config.txt"), Path("/boot/config.txt")) if p.exists()), None)
    if cfgtxt:
        txt = cfgtxt.read_text()
        for need in (f"dtoverlay=tesseract-audio,amp_gpio={cfg.pins.amp_enable or cfg.pins.amp_enable_dummy}",
                     "dtparam=spi=on",
                     f"dtoverlay=spi0-1cs,cs0_pin={cfg.pins.display_cs}", "dtparam=audio=off"):
            mark = "ok" if need in txt else "MISSING (run install.sh)"
            ok &= need in txt
            print(f"  {need:28s} {mark}")
    else:
        print("  (not a Raspberry Pi)")

    print("── sound card")
    try:
        print("  " + Path("/proc/asound/cards").read_text().strip().replace("\n", "\n  "))
        from .audio import find_alsa_device
        print(f"  -> using {find_alsa_device(cfg.audio.device)}")
    except OSError:
        print("  (no ALSA)")

    print("── OpenRouter")
    if not cfg.openrouter_key:
        print("  ! OPENROUTER_API_KEY missing from .env")
        ok = False
    else:
        import httpx
        try:
            r = httpx.get("https://openrouter.ai/api/v1/key", timeout=10,
                          headers={"Authorization": f"Bearer {cfg.openrouter_key}"})
            if r.status_code == 200:
                d = r.json().get("data", {})
                print(f"  key ok (usage ${d.get('usage', 0):.2f}, limit {d.get('limit')})")
            else:
                print(f"  ! key rejected: HTTP {r.status_code}")
                ok = False
        except Exception as e:
            print(f"  ! can't reach openrouter.ai: {e}")
            ok = False
    print(f"  stt={cfg.stt.model}  tts={cfg.tts.model} ({cfg.tts.voice})")
    print(f"  fast={cfg.llm.model} via {cfg.llm.provider_order or cfg.llm.provider_sort or 'default routing'}")
    print(f"  thinking={cfg.thinking.model} via {cfg.thinking.provider_order or cfg.thinking.provider_sort or 'default routing'}")

    print("── wake word")
    model = cfg.path(cfg.wake.model)
    if cfg.wake.engine in ("sherpa", "both"):
        from .wakeword import KWS_FILES, KWS_MODEL
        d = cfg.path("models") / KWS_MODEL
        have = all((d / f).exists() for f in KWS_FILES)
        print(f"  listening for \"{cfg.wake.keyword or cfg.assistant.name}\": "
              f"{'ok' if have else '! model missing (run install.sh)'}")
        ok &= have
    if cfg.wake.engine in ("openwakeword", "both"):
        print(f"  {model}: {'ok' if model.exists() else 'not trained yet -> using ' + cfg.wake.fallback_model}")
    if cfg.wake.engine == "porcupine":
        print(f"  porcupine keyword {cfg.path(cfg.wake.porcupine_keyword)}")

    print("── Wi-Fi and location")
    from . import settings
    if settings.Wifi.available():
        try:
            w = settings.Wifi()
            connected, conn = w.status()
            print(f"  {'connected to ' + conn if connected else '! not connected'}; saved: "
                  + (", ".join(ssid for _, ssid in w.saved()) or "none"))
        except Exception as e:
            print(f"  ! nmcli failed: {e}")
    if cfg.assistant.location:
        print(f"  location {cfg.assistant.location}, time zone {settings.current_timezone() or '?'}")
    else:
        print('  · location not set (the weather needs it): say "Franklin, I\'m in <city>" or run '
              'tesseract location "<city, state>"')

    print("── agents")
    if not cfg.agents:
        print(f"  none configured: background jobs use the built-in agent ({cfg.thinking.model})")
    for a in cfg.agents:
        key = "" if not a.api_key_env else (" key ok" if a.api_key else f" ! {a.api_key_env} missing")
        reach = ""
        if a.url:
            import httpx
            try:
                r = httpx.get(a.url.rstrip("/") + ("/models" if a.type in ("openai", "hermes") else ""), timeout=4,
                              headers={"Authorization": f"Bearer {a.api_key}"} if a.api_key else {})
                reach = " ! key rejected (HTTP 401)" if r.status_code in (401, 403) else " reachable"
            except Exception:
                reach = " ! unreachable"
        print(f"  {a.name} ({a.type}){reach}{key}")
    if cfg.agents and cfg.thinking.keep_builtin:
        print(f"  {cfg.assistant.name} (built-in: on-device to-do list, calendar, notes; [thinking] keep_builtin)")
    print("\nall good" if ok else "\nfix the ! items above")
    return 0 if ok else 1


def _app_parts(cfg, sim):
    from . import dsp
    from .audio import AudioIO
    from .cloud import OpenRouter
    from .playback import Mixer
    audio = AudioIO(cfg, sim=sim)
    mixer = Mixer(audio, lambda: dsp.volume_to_gain(cfg.audio.volume, cfg.audio.max_volume_db))
    mixer.start()
    return audio, mixer, OpenRouter(cfg)


def cmd_say(cfg, text, sim) -> int:
    from .speech import SpeechSession
    _, mixer, cloud = _app_parts(cfg, sim)
    t0 = time.monotonic()
    first = []
    s = SpeechSession(cloud, mixer, cfg, threading.Event(), on_audio=lambda: first.append(time.monotonic()))
    s.say(text)
    s.finish()
    mixer.close()
    if s.error:
        print(f"error: {s.error}")
        return 1
    print(f"first audio after {first[0] - t0:.2f}s" if first else "no audio")
    return 0


def _throttled() -> int | None:
    """The Pi's power flags (vcgencmd get_throttled), or None off the Pi."""
    import subprocess
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip().split("=")[1], 16)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def cmd_speaker(cfg, arg: str) -> int:
    """Is the crackle the speaker, the power, or the software? Clean test sounds, no network involved."""
    import numpy as np

    from .audio import AlsaPlayback
    from .playback import ArraySource
    if arg:
        cfg.audio.volume = max(0, min(10, int(arg)))
    sr = 48_000
    before = _throttled()
    audio, mixer, _ = _app_parts(cfg, False)
    time.sleep(0.5)
    AlsaPlayback.underruns = 0

    def play(label, x, listen):
        print(f"\n{label}\n   listen for: {listen}")
        src = mixer.play(ArraySource(x.astype(np.float32)))
        while not src.finished:
            time.sleep(0.05)
        time.sleep(0.3)

    t = np.arange(int(sr * 3)) / sr
    play("1. A steady tone, 3 s", 0.5 * np.sin(2 * np.pi * 440 * t),
         "any click, crackle or buzz. A clean tone should be perfectly smooth.")
    secs = 6.0
    t = np.arange(int(sr * secs)) / sr
    f = 80 * (8000 / 80) ** (t / secs)
    play("2. A sweep from low to high, 6 s", 0.5 * np.sin(2 * np.pi * np.cumsum(f) / sr),
         "a rattle or buzz at the low end (the start). That's the speaker cone or something loose.")
    t = np.arange(int(sr * 5)) / sr
    play("3. A low tone getting louder, 5 s", (t / 5) * np.sin(2 * np.pi * 150 * t),
         "the moment it starts to crackle. Only when loud = overdriven speaker or the power dipping.")
    mixer.close()
    after = _throttled()

    print("\n── results")
    u = AlsaPlayback.underruns
    print(f"sound card dropouts while playing: {u}  " + ("ok" if not u else "<-- the software kept the sound card waiting"))
    if after is None:
        print("power: can't read (vcgencmd isn't available)")
    else:
        new = after & ~(before or 0)
        if after & 0x1 or new & 0x10000:
            print(f"power: UNDER-VOLTAGE (get_throttled=0x{after:x}). The 5 V supply sags when the amp pulls current,")
            print("       which pops and crackles. Use a stronger supply (5 V 3 A) and a short, thick cable.")
        elif after & 0x10000:
            print(f"power: it dipped at some point since boot (0x{after:x}), but not during this test")
        else:
            print("power: ok (no under-voltage)")
    print("\nIf all three were clean, the speaker is fine and the crackle came from the voice stream or the")
    print("software: check `journalctl -u tesseract | grep -E 'ran dry'` after a crackly reply.")
    print("If 1 or 2 crackled at a normal volume with no dropouts and no under-voltage: check the speaker's")
    print("wires and solder joints, then the speaker itself (a rubbing voice coil buzzes on the sweep).")
    return 0


def cmd_interrupt_test(cfg) -> int:
    """Can you interrupt it? It talks for ~30 s while you say "Franklin". The wake word listens the way it
    does in the speaker, and everything (mic, what the speaker played, the echo-cancelled result) is saved
    to data/interrupt-test.npz for a closer look."""
    import numpy as np

    from .audio import AlsaCapture, AlsaPlayback
    from .speech import SpeechSession
    from .wakeword import make_detector
    audio, mixer, cloud = _app_parts(cfg, False)
    echo = audio.echo
    if echo is None:
        print("Echo cancelling is off ([audio] echo_cancel = false, or no sound card): it can't hear you over itself.")
    det = make_detector(cfg)                  # before the mic starts: loading it takes a while on the Pi
    clean = audio.subscribe()
    if echo is not None:
        echo.tape = {"far": [], "mic": [], "out": []}
    audio.start()
    threshold = min(0.98, cfg.wake.threshold + cfg.wake.speaking_threshold_boost)
    heard, ignored, stop = [], [], threading.Event()
    t0 = time.monotonic()

    def listen():                             # its own thread, like the speaker's wake word listener
        quiet = 0.0
        while not stop.is_set():
            try:
                frames = [clean.get(timeout=0.1)]
                while not clean.empty():
                    frames.append(clean.get_nowait())
            except Exception:
                continue
            if det.process(np.concatenate(frames)) >= threshold and time.monotonic() > quiet:
                quiet = time.monotonic() + 1.5
                det.reset()
                if echo is not None and echo.someone_talked(at=getattr(frames[-1], "index", None)) is False:
                    print(f"  {time.monotonic() - t0:5.1f} s  ignored: that was its own voice, nobody talked over it")
                    ignored.append(time.monotonic() - t0)
                    continue
                heard.append(time.monotonic() - t0)
                print(f"  {heard[-1]:5.1f} s  heard you")
    threading.Thread(target=listen, daemon=True).start()
    time.sleep(1)
    AlsaCapture.overruns = AlsaPlayback.underruns = 0
    s = SpeechSession(cloud, mixer, cfg, threading.Event())
    s.say("I'm going to keep talking for a little while, so you can try interrupting me. Every few seconds, say "
          "my name, the way you normally would, from where you'd normally stand. Here's a story while you do. "
          "There was once a small cube that lived on a desk. It told the time, it played music, and it answered "
          "questions all day long. But it had one problem: when it was talking, it could barely hear anyone "
          "else, because its own speaker was right next to its ears. So it learned to listen past its own "
          "voice. Did it work? Let's find out. Keep saying my name until I stop talking. Thanks for helping.")
    s.finish(wait=False)
    print('Franklin is about to talk for ~30 s. Say "Franklin" every few seconds while it does.\n')
    while not s.source.done.wait(0.2) and time.monotonic() - t0 < 90:
        pass
    time.sleep(1.0)
    stop.set()
    mixer.close()
    print("\n── results")
    print(f"  heard you {len(heard)} time(s); ignored its own voice {len(ignored)} time(s)")
    print(f"  mic lost audio {AlsaCapture.overruns} time(s), speaker ran dry {AlsaPlayback.underruns} time(s)"
          + ("  <-- timing problem: the echo cancelling can't stay lined up" if AlsaCapture.overruns
             + AlsaPlayback.underruns else ""))
    if echo is None:
        return 0
    print("  " + echo.summary())
    tape = echo.tape
    echo.tape = None
    mic = np.concatenate([x for _, x in tape["mic"]]) if tape["mic"] else np.zeros(1, np.float32)
    peak = float(np.abs(mic).max()) / audio.gain                 # before [audio] mic_gain_db
    print(f"  loudest the mic got: {20 * np.log10(peak + 1e-9):.1f} dBFS"
          + ("  <-- at the top: the mic is clipping on the speaker" if peak > 0.89 else ""))
    out = cfg.path(cfg.system.state_dir) / "interrupt-test.npz"
    np.savez_compressed(out, mic=mic, mic_start=tape["mic"][0][0] if tape["mic"] else 0,
                        out=np.concatenate(tape["out"]) if tape["out"] else np.zeros(1, np.float32),
                        far=np.concatenate([x for _, x in tape["far"]]) if tape["far"] else np.zeros(1, np.float32),
                        far_start=tape["far"][0][0] if tape["far"] else 0, delay=echo.delay if echo.delay else 0,
                        gain=audio.gain, heard=np.array(heard))
    print(f"\nSaved the recording to {out}. To send it over, on your Mac:")
    print(f"  scp {os.environ.get('SUDO_USER') or 'pi'}@tesseract.local:{out} ~/Downloads/")
    return 0


def cmd_ask(cfg, text, sim) -> int:
    """The fast model, and the background agent if it hands the job off (its questions are asked here)."""
    from .agents import AgentManager
    from .alarms import Alarms
    from .assistant import Assistant
    from .organizer import Organizer
    from .speech import SpeechSession
    from .thinker import ThinkingAgent
    from .weather import Weather
    _, mixer, cloud = _app_parts(cfg, sim)

    class Hooks:
        def set_volume(self, v): print(f"[volume -> {v}]")
        def volume(self): return cfg.audio.volume
        def enter_agent_mode(self, n): print(f"[agent mode -> {n}]")
        def set_working(self, label): label and print(f"[{label}]")

    state = cfg.path(cfg.system.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    alarms, weather = Alarms(state / "alarms.json", on_fire=print), Weather(cfg)
    builtin = ThinkingAgent(cfg, cloud, Organizer(state / "organizer.json"), alarms, weather)
    agents = AgentManager(cfg, on_finished=lambda t: None, builtin=builtin,
                          on_question=lambda t: agents.answer(input(f"[{t.agent} asks] {t.question}\n> "), t))
    asst = Assistant(cfg, cloud, alarms, agents, Hooks(), weather=weather)
    cancel = threading.Event()
    t0 = time.monotonic()
    first = []
    s = SpeechSession(cloud, mixer, cfg, cancel, on_audio=lambda: first.append(time.monotonic()))
    reply = asst.respond(text, s, cancel)
    t1 = time.monotonic()
    s.finish()
    print(reply)
    print(f"\nLLM done {t1 - t0:.2f}s, first audio {first[0] - t0:.2f}s" if first else "")
    for task in agents.running():
        print(f"\n[background job: {task.request}]")
        while not task.done.wait(1):
            print(f"  {task.fraction:4.0%} {task.progress}")
        print(f"[{task.status} after {task.finished - task.started:.1f}s] {task.result}")
        s = SpeechSession(cloud, mixer, cfg, cancel)
        s.say(task.result)
        s.finish()
    mixer.close()
    return 0


def cmd_record(cfg, secs, sim) -> int:
    import numpy as np
    from . import dsp
    from .audio import RATE
    from .playback import ArraySource
    audio, mixer, _ = _app_parts(cfg, sim)
    audio.start()
    q = audio.subscribe()
    frames, t_end, peak = [], time.monotonic() + secs, 0
    print(f"recording {secs}s ... talk!")
    while time.monotonic() < t_end:
        f = q.get()
        frames.append(f)
        peak = max(peak, int(np.abs(f.astype(np.int32)).max()))
        bar = "#" * int(audio.level * 40)
        print(f"\r  level {bar:40s} peak so far {peak:5d}", end="", flush=True)
    x = np.concatenate(frames)
    clipped = 100 * float(np.mean(np.abs(x.astype(np.int32)) >= 32000))
    rms_db = 20 * np.log10(dsp.rms(x) / 32768 + 1e-9)
    print(f"\n  rms {rms_db:.1f} dBFS, peak {peak} of 32767, {clipped:.1f}% clipped")
    if clipped > 0.1 or rms_db > -20:
        print(f"  too loud: lower [audio] mic_gain_db (now {cfg.audio.mic_gain_db:g}) by about {rms_db + 27:.0f}")
    elif rms_db < -35:
        print(f"  too quiet: raise [audio] mic_gain_db (now {cfg.audio.mic_gain_db:g}) by about {-27 - rms_db:.0f}")
    else:
        print("  good level (speech at arm's length should be about -35 to -20 dBFS, nothing clipped)")
    from .audio import to_wav_bytes
    out = cfg.path(cfg.system.state_dir) / "record.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(to_wav_bytes(x))
    print(f"  saved {out}; playing back")
    y = dsp.StreamResampler(RATE, 48000).process(x.astype(np.float32) / 32768)
    mixer.play(ArraySource(y))
    mixer.close(timeout=secs + 5)          # waits for the playback, then stops the card cleanly
    return 0


def cmd_wake(cfg, sim) -> int:
    from .audio import AudioIO
    from .wakeword import WakeWordListener
    audio = AudioIO(cfg, sim=sim)
    w = WakeWordListener(cfg, audio, on_wake=lambda s: print(f"\n*** WAKE ({s:.2f}) ***"))
    audio.start()
    w.start()
    print(f"model {w.model_name}, threshold {cfg.wake.threshold}. Say it! (Ctrl-C to quit)")
    peak = 0.0
    try:
        while True:
            time.sleep(0.1)
            peak = max(peak * 0.97, w.last_score)
            print(f"\r  score {w.last_score:.2f}  recent peak {peak:.2f}  mic {'#' * int(audio.level * 30):30s}",
                  end="", flush=True)
    except KeyboardInterrupt:
        w.stop()
        print()
        return 0


def cmd_buttons(cfg) -> int:
    from gpiozero import Button
    p = cfg.pins
    held = []      # gpiozero stops watching a pin once its Button is garbage-collected
    for name, pin in (("SW4 action", p.button_action), ("SW3 volume up", p.button_vol_up),
                      ("SW1 volume down", p.button_vol_down), ("SW2 mute", p.button_mute)):
        b = Button(pin, pull_up=True, bounce_time=0.03)
        b.when_pressed = lambda n=name, g=pin: print(f"  {n} (GPIO{g}) pressed", flush=True)
        b.when_released = lambda n=name, g=pin: print(f"  {n} (GPIO{g}) released", flush=True)
        held.append(b)
    print("press each button (Ctrl-C to quit)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def cmd_brightness(cfg, arg: str) -> int:
    """Change the screen brightness (or night mode) in data/settings.json and tell the running speaker."""
    import datetime as dt
    import json
    import subprocess

    from .util import next_at
    path = cfg.path(cfg.system.state_dir) / "settings.json"
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        saved = {}
    level = int(saved.get("screen", cfg.display.brightness))
    now = dt.datetime.now()
    if arg in ("night", "goodnight"):
        saved.update(night_until=next_at(cfg.night.sunrise_end, now).timestamp(), day_until=0.0)
    elif arg in ("morning", "day"):
        until = next_at(cfg.night.start, now) if cfg.night.start else now + dt.timedelta(hours=18)
        saved.update(night_until=0.0, day_until=until.timestamp())
    elif arg in ("up", "down"):
        saved["screen"] = max(1, min(10, level + (3 if arg == "up" else -3)))
    elif arg.isdigit():
        saved["screen"] = max(0, min(10, int(arg)))
    elif arg:
        print("usage: tesseract brightness [0-10 | up | down | night | morning]")
        return 2
    if arg:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(saved))
        subprocess.run(["systemctl", "kill", "-s", "USR2", "tesseract"], check=False, capture_output=True)
    night = saved.get("night_until", 0) > now.timestamp()
    print(f"screen brightness {saved.get('screen', level)}/10" + (", night mode on" if night else ""))
    return 0


def _restart_service() -> None:
    """Config changed: restart the speaker if it's running so it picks the change up."""
    import subprocess
    try:
        subprocess.run(["systemctl", "try-restart", "tesseract"], check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def cmd_perf(cfg) -> int:
    """Measure each part on its own, then all together, counting sound-card dropouts."""
    import datetime as dt
    import os

    import numpy as np
    from .audio import AlsaCapture, AlsaPlayback
    from .display import Display
    from .leds import Leds, to_pixels
    from .state import State, Status
    from .wakeword import WakeWordListener, make_detector

    def dropouts(label, secs=8):
        AlsaCapture.overruns = AlsaPlayback.underruns = 0
        time.sleep(secs)
        o, u = AlsaCapture.overruns, AlsaPlayback.underruns
        print(f"  {label}: {o} mic overruns, {u} speaker underruns in {secs} s  {'ok' if o + u == 0 else '<-- PROBLEM'}")
        return o + u

    print(f"cores: {os.cpu_count()}, load average: {os.getloadavg()[0]:.2f}")
    print("1. sound card alone (mic recording, speaker channel open, nothing else)")
    audio, mixer, _ = _app_parts(cfg, False)
    audio.start()
    q = audio.subscribe()
    drain = threading.Event()
    threading.Thread(target=lambda: [q.get() for _ in iter(drain.is_set, True)], daemon=True).start()
    alone = dropouts("sound card")

    print("2. wake word, 10 s of audio")
    from .wakeword import EitherDetector
    det = make_detector(cfg)
    noise = np.random.default_rng(0).normal(0, 300, 16000 * 10).astype(np.int16)
    for part in det.detectors if isinstance(det, EitherDetector) else (det,):   # each gets its own core
        t0, longest = time.perf_counter(), 0.0
        for i in range(0, len(noise), 320):
            a = time.perf_counter()
            part.process(noise[i:i + 320])
            longest = max(longest, time.perf_counter() - a)
        took = time.perf_counter() - t0
        print(f"  {part.name}: {100 * took / 10:.0f}% of one core, longest call {longest * 1000:.0f} ms"
              f"  {'ok' if took < 5 else '<-- too slow: it will fall behind the mic'}")

    print("3. LEDs, 100 frames (budget at 50 fps: 20 ms each)")
    st = Status()
    st.set(state=State.LISTENING)
    leds = Leds(cfg, st, mic_level=lambda: 0.5, speaker_level=lambda: 0.5)
    t0 = time.perf_counter()
    for i in range(100):
        leds.strip.show(to_pixels(np.clip(leds._scene(st, i / 50, i / 50), 0, 1), cfg.leds.brightness))
    ms = (time.perf_counter() - t0) * 10
    print(f"  {ms:.1f} ms per frame = {ms / 20 * 100:.0f}% of one core  {'ok' if ms < 6 else '<-- heavy'}")
    print(f"3b. sound card while the LEDs animate (LED driver on DMA channel {cfg.leds.dma})")
    leds.start()
    with_leds = dropouts("sound card + LEDs")
    leds.off()
    time.sleep(1)

    print("4. display, 30 animated frames (budget at 15 fps: 67 ms each)")
    disp = Display(cfg, st)
    t0 = time.perf_counter()
    for i in range(30):
        st.set(state=State.THINKING)
        img = disp.render(st, dt.datetime.now())
        if disp.panel is not None:
            disp.panel.show(img)
        time.sleep(0.001 * (i % 2))
    ms = (time.perf_counter() - t0) / 30 * 1000
    print(f"  {ms:.1f} ms per frame = {ms / 67 * 100:.0f}% of one core at 15 fps  {'ok' if ms < 25 else '<-- heavy'}")

    print("5. everything together, like `tesseract run` (idle)")
    leds.start()
    disp.start()
    WakeWordListener(cfg, audio, on_wake=lambda s: None).start()
    st.set(state=State.IDLE)
    together = dropouts("all running")
    drain.set()
    leds.off()
    disp.stop()
    mixer.close(timeout=1)
    if alone:
        print("\n=> the sound card drops out even on its own: a driver/setup problem, not CPU")
    elif with_leds:
        print(f"\n=> the LED driver breaks the sound card: its DMA channel ({cfg.leds.dma}) collides with the "
              "audio's. Try [leds] dma = 13 (then 14, 11, 12) and run this again.")
    elif together:
        print("\n=> fine alone, drops out with everything running: look at whichever part is marked above")
    else:
        print("\n=> no dropouts")
    return 0


# male-sounding voices worth hearing first: (model, voice)
VOICES = [("microsoft/mai-voice-2.1-flash", f"en-US-{n}:MAI-Voice-2.1-Flash") for n in ("Ethan", "Grant", "Jasper")] + \
         [("microsoft/mai-voice-2.1", f"en-US-{n}:MAI-Voice-2.1") for n in ("Ethan", "Grant", "Jasper")] + \
         [("google/gemini-3.8-flash-tts", n) for n in ("Charon", "Puck", "Orus", "Fenrir", "Iapetus")]
SAMPLE = "Hi, I'm Franklin. Your pasta timer has four minutes left, and tomorrow looks sunny, with a high of seventy five."


def cmd_voices(cfg, model: str, sim) -> int:
    """Audition voices: `tesseract voices` for a shortlist, or `tesseract voices MODEL` for all of one model's."""
    import httpx
    from .speech import SpeechSession
    voices = VOICES
    if model:
        m = next((m for m in httpx.get("https://openrouter.ai/api/v1/models?output_modalities=all", timeout=15)
                  .json()["data"] if m["id"] == model), None)
        if not m or not m.get("supported_voices"):
            print(f"no voices listed for {model}")
            return 1
        voices = [(model, v) for v in m["supported_voices"] if ":" not in v or v.startswith("en-")]
    _, mixer, cloud = _app_parts(cfg, sim)
    for i, (m, v) in enumerate(voices, 1):
        cfg.tts.model, cfg.tts.voice = m, v
        print(f"[{i}/{len(voices)}] {m}  {v}", flush=True)
        t0, first = time.monotonic(), []
        s = SpeechSession(cloud, mixer, cfg, threading.Event(), on_audio=lambda: first.append(time.monotonic()))
        s.say(SAMPLE)
        s.finish()
        print(f"        {'error: ' + str(s.error) if s.error else f'first audio after {first[0] - t0:.2f}s' if first else 'no audio'}")
        time.sleep(0.8)
    mixer.close()
    print('\nTo use one, set both lines in [tts] in config.toml, e.g.\n  model = "google/gemini-3.8-flash-tts"\n'
          '  voice = "Charon"')
    return 0


def cmd_location(cfg, place: str) -> int:
    from .settings import current_timezone, set_location
    from .weather import Weather
    if not place:
        print(f"location: {cfg.assistant.location or '(not set)'}; time zone: {current_timezone() or '?'}")
        return 0
    try:
        print(set_location(cfg, place, Weather(cfg)))
    except LookupError as e:
        print(f"error: {e}")
        return 1
    _restart_service()
    return 0


def cmd_wifi(cfg, args, user) -> int:
    from getpass import getpass
    from . import settings
    if not settings.Wifi.available():
        print("Wi-Fi needs NetworkManager (nmcli): run this on the Pi.")
        return 1
    w = settings.Wifi()
    sub = args[0] if args else "list"
    if sub == "add" and len(args) >= 2:
        ssid = w.match(args[1])
        if ssid != args[1]:
            print(f"Using the network in range called {ssid!r}.")
        if user:
            print(f"School/work Wi-Fi: signing in as {user}.")
        pw = args[2] if len(args) > 2 else getpass(f"password for {ssid} (Enter for an open network): ")
        w.add(ssid, pw, user or "")
        print(f"Saved {ssid}. Tesseract joins it by itself whenever it's in range; nothing switches now.")
    elif sub == "forget" and len(args) >= 2:
        n = w.forget(args[1])
        print(f"Forgot {args[1]}." if n else f"No saved network called {args[1]}.")
    elif sub == "setup":
        import subprocess
        r = subprocess.run(["systemctl", "kill", "-s", "USR1", "tesseract"], check=False)
        print("Opening the setup hotspot: look at Tesseract's screen. (This SSH session will drop.)"
              if r.returncode == 0 else "The tesseract service isn't running; start it first.")
    elif sub == "list":
        connected, conn = w.status()
        print(f"now: {'connected to ' + conn if connected else 'not connected'}")
        print("saved:  " + (", ".join(ssid for _, ssid in w.saved()) or "none"))
        print(f"MAC address (to register on campus IoT Wi-Fi): {w.mac() or 'unknown'}")
        print("in range:")
        for n in w.scan()[:12]:
            print(f"  {n.signal:3d}%  {n.ssid}{'' if n.secure else '  (open)'}")
    else:
        print('usage: tesseract wifi [add NAME [PASSWORD] [--user USERNAME] | forget NAME | setup]')
        return 2
    return 0


def _v1_url(url: str, port: int) -> str:
    """"192.168.1.50" -> "http://192.168.1.50:8642/v1"; "https://host" keeps 443 (e.g. tailscale serve)."""
    import urllib.parse
    if "://" not in url:
        url = "http://" + url
    u = urllib.parse.urlsplit(url)
    netloc = u.netloc if u.port or u.scheme == "https" else f"{u.netloc}:{port}"
    path = u.path.rstrip("/")
    return f"{u.scheme}://{netloc}{path if path.endswith('/v1') else path + '/v1'}"


def _list_models(url: str, key: str = "") -> list[str]:
    import httpx
    r = httpx.get(url + "/models", headers={"Authorization": f"Bearer {key}"} if key else {}, timeout=8)
    r.raise_for_status()
    return [m.get("id", "") for m in r.json().get("data", [])]


HERMES_ENV_FILE = Path("/etc/tesseract/franklin.env")   # optional: FRANKLIN_API_KEY=... (root-only)


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    pairs = (ln.split("=", 1) for ln in lines if "=" in ln and not ln.lstrip().startswith("#"))
    return {k.strip().removeprefix("export ").strip(): v.strip().strip("'\"") for k, v in pairs}


def cmd_hermes(cfg, args) -> int:
    """Connect a Hermes Agent API server: save the key in .env and an [[agents]] block in data/local.toml."""
    from getpass import getpass
    from . import settings
    if args and args[0] in ("off", "remove", "forget"):
        settings.set_local_agent(cfg, "Hermes", None)
        print("Disconnected Hermes; background jobs go to the built-in agent again.")
        _restart_service()
        return 0
    if not args:
        hermes = next((a for a in cfg.agents if a.name.lower() == "hermes"), None)
        saved = _read_env_file(HERMES_ENV_FILE).get("FRANKLIN_URL")
        if not hermes and saved:
            return cmd_hermes(cfg, [saved])
        print(f"Hermes: {hermes.url} (key {'set' if hermes.api_key else 'MISSING'})" if hermes else
              "Hermes isn't connected. Usage: tesseract hermes <its address>, e.g.\n"
              "  tesseract hermes 192.168.1.50                (port 8642 and /v1 are added for you)\n"
              "  tesseract hermes https://franklin.<tailnet>.ts.net   (tailscale serve, port 443)")
        return 0
    url = _v1_url(args[0], 8642)
    saved = _read_env_file(HERMES_ENV_FILE)
    key = saved.get("FRANKLIN_API_KEY") or saved.get("HERMES_API_KEY") or ""
    if key:
        print(f"Using the API key in {HERMES_ENV_FILE}.")
    else:
        key = getpass("Hermes API key (its API_SERVER_KEY): ").strip()
    print(f"Checking {url} ...")
    try:
        models = _list_models(url, key)
    except Exception as e:
        print(f"Couldn't reach Hermes at {url}: {e}\n"
              "On the Hermes machine: API_SERVER_ENABLED=true in ~/.hermes/.env, `hermes gateway` running, and\n"
              "either API_SERVER_HOST=0.0.0.0 (same network) or `tailscale serve` with this Pi on the tailnet\n"
              "(check with: tailscale status). HTTP 401 means the key is wrong.")
        return 1
    print("Models: " + (", ".join(models) or "none listed"))
    import httpx
    try:
        r = httpx.post(url + "/chat/completions", headers={"Authorization": f"Bearer {key}"}, timeout=90,
                       json={"model": "hermes-agent", "messages": [{"role": "user", "content": "Reply with just: OK"}]})
        r.raise_for_status()
        print("Hermes says: " + r.json()["choices"][0]["message"]["content"].strip()[:80])
    except Exception as e:
        print(f"Hermes is there but the test request failed ({e}). Saving anyway.")
    settings.set_env_value(cfg.base_dir / ".env", "HERMES_API_KEY", key)
    settings.set_local_agent(cfg, "Hermes", {
        "name": "Hermes", "type": "openai", "url": url, "api_key_env": "HERMES_API_KEY", "model": "hermes-agent",
        "description": "Hermes, my personal AI agent on a cloud server: my Google Calendar (if connected), web "
                       "research, long-term memory and our past conversations. Use it for anything about my "
                       "schedule, research, or what it knows about me.",
        "session": "tesseract", "timeout_s": 900})
    print("Connected. Background jobs now go to Hermes. Try: \"Franklin, ask Hermes what's on my calendar\" "
          "or \"Franklin, let me talk to Hermes\".")
    _restart_service()
    return 0


def cmd_local(cfg, args) -> int:
    """Use a model on your own computer (LM Studio, Ollama, ...) for the fast answers."""
    from . import settings
    L = cfg.local_llm
    if not args:
        state = "ON" if L.enabled else "off"
        print(f"local model: {state}  {L.model or '(none)'} at {L.url or '(not set)'}  (used for: {L.use_for})")
        if not L.url:
            print("Set one up: tesseract local <your computer's address> [model], e.g.\n"
                  "  tesseract local my-macbook.local        (LM Studio's port 1234 and /v1 are added for you)")
        return 0
    if args[0] in ("on", "off"):
        if args[0] == "on" and not (L.url and L.model):
            print("Set it up first: tesseract local <address> [model]")
            return 1
        settings.set_local_value(cfg, "local_llm", "enabled", args[0] == "on")
        print("Answers now come from " + (f"{L.model} on your computer." if args[0] == "on" else "the cloud."))
        _restart_service()
        return 0
    url = _v1_url(args[0], 1234)
    print(f"Checking {url} ...")
    try:
        models = [m for m in _list_models(url) if "embed" not in m.lower()]
    except Exception as e:
        print(f"Couldn't reach {url}: {e}\n"
              "In LM Studio: Developer tab -> Status: Running, and turn on \"Serve on Local Network\".\n"
              "Both machines must be on the same network.")
        return 1
    want = args[1] if len(args) > 1 else ""
    pick = next((m for m in models if m == want), None) or next((m for m in models if want and want.lower() in m.lower()), None)
    if pick is None:
        if want or len(models) != 1:
            print("Models it has: " + (", ".join(models) or "none loaded"))
            print("Run again with one of them: tesseract local " + args[0] + " <model>")
            return 1
        pick = models[0]
    settings.set_local_value(cfg, "local_llm", "url", url)
    settings.set_local_value(cfg, "local_llm", "model", pick)
    settings.set_local_value(cfg, "local_llm", "enabled", True)
    print(f"Using {pick} on your computer for answers. Speech-to-text, the voice and web search stay in the "
          "cloud.\nIf the computer sleeps or you leave, it uses the cloud models until it's back. "
          "`tesseract local off` (or \"Franklin, use the cloud\") switches back.")
    _restart_service()
    return 0


def cmd_spotify(cfg, args) -> int:
    """Connect Spotify: Spotify Soloist plays it, a developer app (PKCE) lets Franklin find things to play."""
    import shutil
    import subprocess
    from getpass import getpass

    from . import settings
    from . import spotify as sp
    path = cfg.path(cfg.system.state_dir) / "spotify.json"
    sub = args[0] if args else "status"
    if sub == "off":
        settings.set_local_value(cfg, "spotify", "enabled", False)
        print("Spotify is off. `tesseract spotify setup` turns it back on.")
        _restart_service()
        return 0
    soloist = sp.soloist_path(cfg)
    if sub == "status":
        api = sp.SpotifyAPI(path)
        try:
            version = sp.soloist_version(soloist) if soloist.exists() else "NOT INSTALLED"
        except sp.SpotifyError as e:
            version = str(e)
        print(f"enabled: {cfg.spotify.enabled}   Web API signed in: {api.linked}   "
              f"Soloist key: {'set' if api.soloist_key else 'NOT SET'}\nSoloist: {version}")
        auth = sp.ask_soloist(cfg)
        if auth is None:
            print("player: not running" + ("" if cfg.spotify.enabled else " (Spotify is off)"))
        elif auth.get("logged_in"):
            print(f"player: signed in, as \"{auth.get('device_name') or cfg.spotify.device_name}\"")
        else:
            print("player: running, not signed in. " + sp.PICK_TESSERACT)
        return 0
    if sub != "setup":
        print("usage: tesseract spotify [setup | off]")
        return 2

    print("Spotify on Tesseract needs Spotify Premium.\n")
    # Soloist only plays to PipeWire or PulseAudio, and Tesseract keeps the sound card: Soloist gets a
    # PulseAudio of its own with no sound card in it (spotify.Player)
    if not (shutil.which("pulseaudio") and shutil.which("parec")):
        print("Installing PulseAudio (Spotify's player plays into it; it never touches the sound card)...")
        subprocess.run(["apt-get", "update", "-qq"], check=False)
        if subprocess.run(["apt-get", "install", "-y", "-qq", "pulseaudio", "pulseaudio-utils"]).returncode != 0:
            print("That didn't work. Try `sudo apt-get install pulseaudio pulseaudio-utils`, then setup again.")
            return 1
    _keep_pulseaudio_private()
    try:
        import websockets  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "websockets>=13"], check=False)
    # raspotify's own librespot would grab the sound card (and show up as a second speaker)
    if subprocess.run(["systemctl", "is-enabled", "raspotify"], capture_output=True, text=True).stdout.strip() \
            in ("enabled", "enabled-runtime"):
        subprocess.run(["systemctl", "disable", "--now", "raspotify"], check=False)
        print("Turned off raspotify's service (`sudo apt remove raspotify` if you like: it isn't used any more).")
    print("Downloading Spotify Soloist, Spotify's own player for speakers like this...")
    try:
        print("   " + sp.install_soloist(soloist))
    except sp.SpotifyError as e:
        print(f"   {e}")
        return 1

    api = sp.SpotifyAPI(path)
    print("\n1. On your laptop, open https://developer.spotify.com/dashboard/soloist and create a Spotify\n"
          "   Soloist API key, signed in with the account that has Premium. Copy it.")
    key = getpass("   Soloist API key (hidden as you paste" +
                  ("; Enter keeps the one you have): " if api.soloist_key else "): ")).strip()
    if key:
        api.set_soloist_key(key)
    elif not api.soloist_key:
        return 1

    me = None
    if api.linked:
        try:
            me = api.call("GET", "/me")
            print(f"\n2. Web API (for finding songs): already signed in as {me.get('display_name') or me.get('id')}.")
        except sp.SpotifyError:
            me = None
    if me is None:
        print("\n2. Now a developer app, so Franklin can search Spotify. On your laptop, go to\n"
              "   https://developer.spotify.com/dashboard -> Create app.\n"
              "     Name/description: anything (e.g. Tesseract)\n"
              f"     Redirect URI: {sp.REDIRECT}   (exactly this, then click Add)\n"
              "     APIs used: tick Web API. Save, then open the app's Settings and copy its Client ID.\n"
              "     Under User Management, add your Spotify account (name and email).")
        client_id = input("   Client ID: ").strip()
        if not client_id:
            return 1
        url, verifier, state = sp.login_url(client_id)
        print("\n3. Open this on your laptop and click Agree:\n\n" + url + "\n\n"
              "   The page it lands on won't load (it's addressed to your laptop, where nothing's listening).\n"
              "   That's expected: copy the whole address from the browser's address bar and paste it here.")
        landed = input("   Address: ").strip()
        try:
            sp.finish_login(path, client_id, verifier, state, landed)
            me = sp.SpotifyAPI(path).call("GET", "/me")
        except sp.SpotifyError as e:
            print(f"   {e}")
            return 1
        print(f"   Signed in as {me.get('display_name') or me.get('id')}.")
    settings.set_local_value(cfg, "spotify", "enabled", True)
    _restart_service()
    print(f"\nLast step, once: with your phone on the same Wi-Fi as Tesseract, open Spotify, tap the devices\n"
          f"button (the speaker icon) and pick \"{cfg.spotify.device_name}\". Soloist keeps that sign-in.\n"
          "Then try \"Franklin, play Blinding Lights by The Weeknd\". `tesseract spotify` shows how it's doing.")
    return 0


def _keep_pulseaudio_private() -> None:
    """Installing PulseAudio mustn't start one that takes the sound card from Tesseract: no per-user
    daemons, and no program starting one by itself when it finds none running."""
    import subprocess
    from pathlib import Path
    subprocess.run(["systemctl", "--global", "mask", "pulseaudio.service", "pulseaudio.socket"],
                   capture_output=True, check=False)
    conf = Path("/etc/pulse/client.conf.d")
    if conf.parent.exists():
        conf.mkdir(exist_ok=True)
        (conf / "90-tesseract.conf").write_text("# Tesseract drives the sound card itself (code/tesseract/spotify.py)\n"
                                                "autospawn = no\n")


def cmd_setup(cfg) -> int:
    """For an SSH session: the location, then any Wi-Fi networks to save for later."""
    from getpass import getpass
    from . import settings
    from .weather import Weather
    print(f"Location now: {cfg.assistant.location or '(not set)'}")
    place = input("Where is Tesseract? (city, state; Enter to keep) ").strip()
    if place:
        try:
            print("  " + settings.set_location(cfg, place, Weather(cfg)))
        except LookupError as e:
            print(f"  {e}")
    if settings.Wifi.available():
        w = settings.Wifi()
        print("Saved Wi-Fi: " + (", ".join(ssid for _, ssid in w.saved()) or "none"))
        while ssid := input("Add a Wi-Fi network for later (its name, or Enter to finish): ").strip():
            pw = getpass("  password (Enter for an open network): ")
            user = input("  username, only for school/work Wi-Fi like eduroam (else Enter): ").strip()
            w.add(ssid, pw, user)
            print(f"  saved {ssid}; it'll join by itself when it's in range")
    _restart_service()
    return 0


def cmd_demo(cfg, what, sim) -> int:
    from .display import Display
    from .leds import Leds
    from .state import State, Status
    st = Status()
    leds = Leds(cfg, st, mic_level=lambda: 0.5 + 0.5 * abs(((time.time() * 1.3) % 2) - 1),
                speaker_level=lambda: 0.5 + 0.5 * abs(((time.time() * 2.1) % 2) - 1), sim=sim)
    disp = Display(cfg, st, mic_level=lambda: 0.6, speaker_level=lambda: 0.7, sim=sim, leds=leds)
    leds.start()
    if what == "display" or sim:
        disp.start()
    demo = [(State.BOOT, {}), (State.IDLE, {}), (State.LISTENING, {}), (State.TRANSCRIBING, {}),
            (State.THINKING, {"user_text": "What's the weather like tomorrow?"}),
            (State.WORKING, {"detail": "Checking the weather"}),
            (State.SPEAKING, {"reply_text": "Tomorrow looks sunny with a high of seventy-two."}),
            (State.IDLE, {"timers": [("pasta", time.time() + 14, 20)]}),
            (State.IDLE, {"task_state": "running", "task_progress": 0.35, "task_label": "Checking your calendar"}),
            (State.IDLE, {"task_state": "running", "task_progress": 0.8, "task_label": "Adding it to your calendar"}),
            (State.IDLE, {"task_state": "waiting"}),
            (State.SPEAKING, {"tint": "done", "reply_text": "Done. The robotics meeting is on your calendar for Friday at six."}),
            (State.IDLE, {"task_state": "done"}), (State.IDLE, {"task_state": "failed"}),
            (State.ERROR, {"detail": "No connection"}), (State.ALARM, {"detail": "Pasta timer"}),
            (State.IDLE, {"night_until": time.time() + 600}),            # "good night": dim and warm
            (State.IDLE, {"muted": True})]
    try:
        for state, kw in demo:
            print(state.value, kw or "")
            st.set(state=state, **{"muted": False, "timers": [], "task_state": "", "tint": "", "night_until": 0.0,
                                   **kw})
            if state == State.IDLE and not kw:
                time.sleep(1)
                st.set(volume=7, volume_changed=time.monotonic())
            time.sleep(3.5)
    finally:
        leds.off()
        disp.stop()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tesseract", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "doctor", "say", "ask", "record", "wake", "buttons", "leds", "display",
                             "setup", "location", "wifi", "voices", "perf", "brightness", "hermes", "local", "spotify", "speaker", "interrupt-test",
                             "routing-test"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--sim", action="store_true", help="run on a laptop instead of the Pi hardware")
    ap.add_argument("--wav", help="feed this WAV file in as the microphone (testing)")
    ap.add_argument("--config", help="path to config.toml")
    ap.add_argument("--user", help="wifi add: username for school/work (eduroam-style) Wi-Fi")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    cfg = config_mod.load(a.config)
    logging.basicConfig(level=logging.DEBUG if a.verbose else getattr(logging, cfg.system.log_level.upper(), 20),
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    text = " ".join(a.args)
    if a.command == "run":
        for pr in check_pins(cfg):
            (logging.info if pr.startswith("note") else logging.error)("pins: %s", pr)
        from .app import Tesseract
        Tesseract(cfg, sim=a.sim, wav_input=a.wav).run()
        return 0
    if a.command == "doctor":
        return cmd_doctor(cfg)
    if a.command == "say":
        return cmd_say(cfg, text or "Hello! I'm Franklin, and I live inside Tesseract.", a.sim)
    if a.command == "ask":
        from .cloud import CloudError
        try:
            return cmd_ask(cfg, text or "What can you do?", a.sim)
        except CloudError as e:
            print(f"error: {e}")
            return 1
    if a.command == "record":
        return cmd_record(cfg, float(text or 4), a.sim)
    if a.command == "wake":
        return cmd_wake(cfg, a.sim)
    if a.command == "buttons":
        return cmd_buttons(cfg)
    if a.command == "setup":
        return cmd_setup(cfg)
    if a.command == "hermes":
        return cmd_hermes(cfg, a.args)
    if a.command == "routing-test":
        from .routing_test import run as routing_test
        return routing_test(cfg, a.args)
    if a.command == "local":
        return cmd_local(cfg, a.args)
    if a.command == "spotify":
        return cmd_spotify(cfg, a.args)
    if a.command == "speaker":
        return cmd_speaker(cfg, text)
    if a.command == "interrupt-test":
        return cmd_interrupt_test(cfg)
    if a.command == "location":
        return cmd_location(cfg, text)
    if a.command == "perf":
        return cmd_perf(cfg)
    if a.command == "brightness":
        return cmd_brightness(cfg, text)
    if a.command == "voices":
        return cmd_voices(cfg, text, a.sim)
    if a.command == "wifi":
        return cmd_wifi(cfg, a.args, a.user)
    return cmd_demo(cfg, a.command, a.sim)


if __name__ == "__main__":
    sys.exit(main())
