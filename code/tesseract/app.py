"""The conductor. Every input (wake word, buttons, alarms, finished agents,
typed text) becomes an event on one queue, handled by one thread, so state
changes never race. A conversation turn runs on a worker thread with its own
cancel flag, which is how barge-in ("Franklin, stop" mid-sentence) and the action
button cut it off instantly.

    wake ─► LISTENING ─► TRANSCRIBING ─► THINKING ─► SPEAKING ─► IDLE
              │ (VAD end-of-speech)        │  ▲ (tool/agent: WORKING)  └─► follow-up LISTENING if Franklin asked a question
              └ nobody spoke ─► IDLE       └──┘

A transcript goes to the first of these that takes it:
    stop phrases ("never mind")          -> done, nothing said
    commands.py (timers, time, volume)   -> answered on the device, no LLM
    a direct line to an external agent   -> straight to that agent
    the fast model (assistant.py)        -> answers, or says "On it." and starts a background job

Background jobs report back through events too: a question ("What time is the
meeting?") is asked out loud as soon as the speaker is free and your next words
go to the job; a result is announced with the LEDs tinted green (done) or red
(failed).
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time

from . import dsp, settings, splash
from .agents import AgentManager
from .alarms import Alarms
from .assistant import FAILED, GOT_IT, HANDOFF, ONE_SEC, Assistant, handoff_phrase
from .audio import HW_RATE, AudioIO
from .buttons import Buttons
from .cloud import CloudError, OpenRouter
from .commands import GOOD_MORNING, GOOD_NIGHT, RESTARTING, SHUTTING_DOWN, WIFI_SETUP, LocalCommands
from .display import Display
from .leds import Leds
from .organizer import Organizer
from .playback import ArraySource, Mixer
from .portal import SetupPortal
from .sounds import Sounds
from .speech import SentenceSplitter, SpeechSession, TTSCache, clean_for_speech
from .state import State, Status
from .thinker import ThinkingAgent
from .util import next_at, spoken_duration
from .vad import record_utterance
from .wakeword import WakeWordListener
from .weather import Weather

log = logging.getLogger(__name__)

# Said on their own, these end the turn instantly without a cloud round trip.
MUSIC_STOP = {"stop", "pause", "stop it", "quiet", "be quiet", "shut up", "enough", "turn it off"}
STOP_PHRASES = {"stop", "cancel", "never mind", "nevermind", "nothing", "shut up", "quiet", "be quiet",
                "thats all", "thats it", "thank you", "thanks", "no thanks", "no thank you", "ok", "okay", ""}
EXIT_AGENT = re.compile(r"\b(back to \w+|go back|switch back|exit|leave|stop talking to|end (the )?"
                        r"conversation|goodbye|bye)\b")
ALARM_MAX_S = 600
# what Whisper invents out of near-silence (a false wake, a cough): treated as nothing said.
# Not "bye": it's also what Whisper makes of a short "hi".
WHISPER_GHOSTS = {"you", "thank you for watching", "thanks for watching", "thanks for watching bye",
                  "please subscribe", "subtitles by the amaraorg community"}
# said in reply to a background job's question: drop the job instead of answering
# quiet mode: asking for what's waiting
READ_HELD = re.compile(r"(read (it|that|them|those|the (results?|answers?))( out)?( to me)?|"
                       r"what (did|have) (you|it|hermes) (find|found|say|said|get|got)( for me)?|"
                       r"whats? (is |was )?(the (results?|answers?)|ready)|what is (the (results?|answers?)|ready)|"
                       r"(tell|give) me( the (results?|answers?))?|go ahead|what (do you have|have you got|you got)"
                       r"( for me)?|(any|whats the) news)( please)?")
DROP_JOB = re.compile(r"(never ?mind|cancel( it| that)?|stop|forget (it|about it)|drop it)")
OFFLINE_FIX_AFTER_S = 120   # offline this long (Wi-Fi up, nothing getting through) -> try to fix it
NEWS_S = 8          # how long the LEDs keep pulsing green/red after a result is read out
# fixed phrases synthesized at startup so they play instantly
SETUP_PROMPT = "I can't find a Wi-Fi network I know. Scan the code on my screen to set one up."
WARM_PHRASES = HANDOFF + [ONE_SEC, GOT_IT, FAILED, "Okay.", "Okay, I'll drop it.", "Timer cancelled.",
                "You don't have a timer running.", WIFI_SETUP, SETUP_PROMPT, GOOD_NIGHT, GOOD_MORNING,
                SHUTTING_DOWN, RESTARTING] + \
               [f"Timer set for {spoken_duration(m * 60)}." for m in (1, 2, 3, 5, 10, 15, 20, 30, 45, 60)]


def normalize(text: str, name: str) -> str:
    t = re.sub(r"[^\w\s']", " ", text.lower()).replace("'", "")
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(rf"^(hey |ok |okay )?{name.lower()}\b ?", "", t).strip()


class Tesseract:
    def __init__(self, cfg, sim: bool = False, wav_input: str | None = None):
        self.cfg = cfg
        self.sim = sim
        self.events: queue.Queue = queue.Queue()
        self.status = Status()
        self.state_dir = cfg.path(cfg.system.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._settings_path = self.state_dir / "settings.json"
        self._apply_saved()

        self.audio = AudioIO(cfg, sim=sim, wav_input=wav_input)
        self.audio.muted = self.status.muted
        self.sounds = Sounds()
        self.mixer = Mixer(self.audio, lambda: dsp.volume_to_gain(self.status.volume, cfg.audio.max_volume_db))
        self.cloud = OpenRouter(cfg)
        self.tts_cache = TTSCache(cfg)
        self.weather = Weather(cfg)
        self._task_news = ("", 0.0)        # ("done" | "failed", until): a result to show on the LEDs
        self.alarms = Alarms(self.state_dir / "alarms.json", on_fire=lambda it: self.events.put(("alarm", it)),
                             on_change=self._refresh_info)
        self.organizer = Organizer(self.state_dir / "organizer.json")
        builtin = ThinkingAgent(cfg, self.cloud, self.organizer, self.alarms, self.weather)
        self.agents = AgentManager(cfg, on_finished=lambda t: self.events.put(("agent_done", t)),
                                   on_change=self._refresh_info,
                                   on_question=lambda t: self.events.put(("task_question", t)), builtin=builtin)
        self.assistant = Assistant(cfg, self.cloud, self.alarms, self.agents, hooks=self, weather=self.weather)
        self.spotify = None              # Spotify on this speaker ([spotify], `tesseract spotify setup`)
        if cfg.spotify.enabled:
            from .spotify import Spotify, SpotifyAPI
            api = SpotifyAPI(self.state_dir / "spotify.json")
            if api.linked and api.soloist_key:
                self.spotify = Spotify(cfg, api, duck=self._duck_music, on_change=lambda now: self.status.set(music=now))
            else:
                log.warning("spotify: enabled but not signed in; run `tesseract spotify setup`")
        self.commands = LocalCommands(cfg, self.alarms, hooks=self)
        if not sim:
            splash.stop()          # the boot light lets go of the LEDs and the screen; we carry on from it
        self.leds = Leds(cfg, self.status, mic_level=lambda: self.audio.level,
                         speaker_level=self.mixer.level, sim=sim, continuing=not sim)
        self.display = Display(cfg, self.status, mic_level=lambda: self.audio.level,
                               speaker_level=self.mixer.level, sim=sim, leds=self.leds)
        self.buttons = Buttons(cfg, on_action=lambda: self.events.put(("action",)),
                               on_action_hold=lambda: self.events.put(("action_hold",)),
                               on_volume=lambda step: self.events.put(("volume", step)),
                               on_mute=lambda: self.events.put(("mute",)), sim=sim,
                               on_setup=lambda: self.events.put(("wifi_setup",)),
                               on_power_off=lambda: self.events.put(("power_off",)))
        # Wi-Fi setup hotspot (needs NetworkManager, so only on the Pi)
        self.portal = None
        if not sim and settings.Wifi.available():
            self.portal = SetupPortal(cfg, settings.Wifi(), self.status, say=self._say_cached,
                                      on_joined=lambda ssid, loc: self.events.put(("joined", ssid, loc)))
        self.wake: WakeWordListener | None = None

        self.turn: threading.Thread | None = None
        self.cancel = threading.Event()
        self.speech: SpeechSession | None = None
        self.alarm_source = None
        self.alarm_started = 0.0
        self.announcements: collections.deque = collections.deque()
        self.held: list = []           # quiet mode: job news waiting until you ask (tap, or "what did you find?")
        self._volume_before_mute = 0
        self._mic_muted_at = 0.0
        self.agent_mode_last = 0.0

    # ── lifecycle ───────────────────────────────────────────────────────────
    def run(self) -> None:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # systemctl stop -> clean LED/display off
            # `tesseract wifi setup` (from an SSH session) opens the setup hotspot in this process
            signal.signal(signal.SIGUSR1, lambda *_: self.events.put(("wifi_setup",)))
            # `tesseract brightness` changed data/settings.json: pick it up
            signal.signal(signal.SIGUSR2, lambda *_: self.events.put(("reload_settings",)))
        self.leds.start()          # rainbow + "Starting" while models load
        self.display.start()
        if not self.sim:           # what the boot light shows next time (follows rotation and colour settings)
            try:
                (self.state_dir / "splash.rgb565").write_bytes(self.display.splash_frame())
            except Exception:
                log.exception("couldn't save the boot screen")
        if not self.cfg.openrouter_key:
            log.error("OPENROUTER_API_KEY is not set (put it in code/.env)")
        self.mixer.start()
        # the speech detector's model, fetched now rather than in the middle of your first request
        threading.Thread(target=self._fetch_vad, name="vad-model", daemon=True).start()
        if self.spotify is not None:
            self.spotify.start(self.mixer)
        # load the wake word models and subscribe before the mic starts, so nothing said at startup is lost
        self.wake = WakeWordListener(self.cfg, self.audio, on_wake=lambda s: self.events.put(("wake", s)),
                                     threshold_boost=self._wake_boost)
        self.wake.start()
        self.audio.start()
        self.alarms.start()
        threading.Thread(target=self._connectivity, name="net", daemon=True).start()
        if self.portal is not None:
            threading.Thread(target=self.portal.watch, name="wifi-setup", daemon=True).start()
        if self.sim:
            threading.Thread(target=self._keyboard, name="keyboard", daemon=True).start()
        self._refresh_info()
        self.status.set(state=State.IDLE)
        log.info("ready: say \"%s\" (wake model: %s)", self.cfg.assistant.name, self.wake.model_name)
        try:
            while True:
                try:
                    ev = self.events.get(timeout=0.5)
                except queue.Empty:
                    ev = None
                if ev and ev[0] == "quit":
                    break
                if ev:
                    try:
                        self._handle(*ev)
                    except Exception:
                        log.exception("event %s failed", ev[0])
                self._housekeeping()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self._cancel_turn()
            if self.wake is not None:
                self.wake.stop()
            self.mixer.close(timeout=1)
            self.leds.off()
            self.display.stop()

    def stop(self) -> None:
        self.events.put(("quit",))

    # ── events ──────────────────────────────────────────────────────────────
    def _handle(self, kind: str, *args) -> None:
        st = self.status.state
        busy = self.turn is not None and self.turn.is_alive()

        if kind == "wake":
            if st == State.ALARM:
                self._stop_alarm()
                self._start_turn(chime=False)
            elif st in (State.LISTENING, State.TRANSCRIBING) and busy:
                return  # said "Franklin" again mid-request
            elif self._hearing_itself():
                log.info("wakeword: ignored; the reply itself said %r", self.cfg.wake.keyword or self.cfg.assistant.name)
                return  # the mic heard our own reply say "Franklin"
            elif self._own_voice():
                log.info("wakeword: ignored; that was its own voice (nobody talked over it)")
                return
            else:
                self._start_turn(preroll=True, chime=self.cfg.sounds.wake_chime)

        elif kind == "action":
            if st == State.ALARM:
                self._stop_alarm()
            elif busy:
                self._cancel_turn()
                self.status.set(state=State.IDLE)
            elif self.held:
                self.release_held()          # quiet mode's green light: a tap reads out what's waiting
            elif self.status.muted:
                self._beep(self.sounds.mute_on)
            else:
                self._start_turn(chime=True)

        elif kind == "action_hold":
            self._stop_alarm()
            self._cancel_turn()
            self.mixer.stop_all()
            self.assistant.forget()
            self.agents.new_conversations()
            self.status.set(state=State.IDLE, agent_mode="")
            self._beep(self.sounds.tick)

        elif kind == "power_off":          # action button held for 5 s
            self._stop_alarm()
            self._cancel_turn()
            self.mixer.stop_all()
            self._say_cached(SHUTTING_DOWN)
            threading.Timer(3.5, self.power_off).start()   # let it finish the sentence

        elif kind == "volume":
            if st == State.ALARM:
                self._stop_alarm()
                return
            self.set_volume(self.status.volume + args[0])
            if st != State.SPEAKING:
                self._beep(self.sounds.tick)

        elif kind == "mute":
            if st == State.ALARM:
                self._stop_alarm()
                return
            muted = not self.status.muted
            if muted and st in (State.LISTENING, State.TRANSCRIBING):
                self._cancel_turn()
                self.status.set(state=State.IDLE)
            self.audio.muted = muted
            self.status.set(muted=muted)
            self._save_settings()
            self._beep(self.sounds.mute_on if muted else self.sounds.mute_off)

        elif kind == "alarm":
            item = args[0]
            self._cancel_turn()
            self.mixer.stop_all()
            snd = self.sounds.timer if item.kind == "timer" else self.sounds.alarm
            src = ArraySource(snd, gain=0.8, loop=True)
            src.min_volume = dsp.volume_to_gain(5, self.cfg.audio.max_volume_db)
            self.alarm_source = self.mixer.play(src)
            self.alarm_started = time.monotonic()
            if self.spotify is not None and self.spotify.playing:
                threading.Thread(target=self._quiet_music, daemon=True).start()
            label = item.label.capitalize() if item.label else ""
            detail = f"{label} timer".strip() if item.kind == "timer" else (label or "Alarm")
            self.status.set(state=State.ALARM, detail=detail)

        elif kind == "agent_done":
            task = args[0]
            # green means one thing: quiet mode is holding a result for you. Otherwise a finished job is just
            # read out (no green "done" flash); a failure keeps its red light until it's been said.
            news = "failed" if task.status == "failed" else ""
            if task.announce and self.status.quiet:      # don't blurt it out: green light until asked
                self.held.append(("result", task))
                news = news or "ready"
            self._task_news = (news, time.monotonic() + (86400 if task.announce else NEWS_S))
            if task.announce and not self.status.quiet:
                self.announcements.append(("result", task))
            self._refresh_info()

        elif kind == "task_question":       # quiet mode: the amber light shows it; it's asked when you check in
            (self.held if self.status.quiet else self.announcements).append(("question", args[0]))

        elif kind == "reload_settings":
            self._apply_saved()

        elif kind == "wifi_setup":
            if self.portal is None:
                log.warning("wifi setup needs NetworkManager (Raspberry Pi OS)")
            else:
                threading.Thread(target=self.portal.toggle, name="wifi-setup", daemon=True).start()

        elif kind == "joined":       # the setup page got it onto a new network
            ssid, location = args
            self.status.set(online=True)
            self._start_turn(announce=("joined", (ssid, location)))

        elif kind == "text":
            self._start_turn(text=args[0])

        elif kind == "followup":
            if not busy and st == State.IDLE and not self.status.muted:
                self._start_turn(follow_up=True, answer_for=args[0] if args else None)

    def _housekeeping(self) -> None:
        busy = self.turn is not None and self.turn.is_alive()
        st = self.status.state
        if st == State.ALARM and time.monotonic() - self.alarm_started > ALARM_MAX_S:
            self._stop_alarm()
        if not busy and st == State.IDLE and self.announcements:
            # a question needs the mic for your answer, so it waits while muted; results don't
            ready = [a for a in self.announcements if a[0] == "result" or not self.status.muted]
            if ready:
                self.announcements.remove(ready[0])
                self._start_turn(announce=ready[0])
        self._refresh_info()
        if self.status.agent_mode and not busy and \
                time.time() - self.agent_mode_last > self.cfg.assistant.memory_minutes * 60:
            log.info("leaving %s mode (inactive)", self.status.agent_mode)
            self.status.set(agent_mode="")

    # ── turns ───────────────────────────────────────────────────────────────
    def _start_turn(self, text=None, preroll=False, chime=False, follow_up=False, announce=None,
                    answer_for=None) -> None:
        self._cancel_turn()
        self.mixer.stop_all()
        cancel = self.cancel = threading.Event()
        pre = self.audio.recent(self.cfg.listen.preroll_ms) if preroll else None
        self.turn = threading.Thread(target=self._turn_main,
                                     args=(cancel, text, pre, chime, follow_up, announce, answer_for),
                                     name="turn", daemon=True)
        self.turn.start()

    def _cancel_turn(self) -> None:
        self.cancel.set()
        if self.speech is not None:
            self.speech.abort()
            self.speech = None

    def _ui(self, cancel: threading.Event, **kw) -> None:
        """Status updates from a worker that may have been superseded."""
        if not cancel.is_set():
            if kw.get("reply_text"):      # the screen shows what's spoken: no markdown or raw links
                kw["reply_text"] = clean_for_speech(kw["reply_text"])
            self.status.set(**kw)

    def _turn_main(self, cancel, text, preroll, chime, follow_up, announce, answer_for) -> None:
        name = self.cfg.assistant.name
        # t_end: about when you stopped talking (for the latency log)
        t_end = time.monotonic()
        try:
            if announce is not None:
                self._announce(cancel, *announce)
                return

            if text is None:
                self._ui(cancel, state=State.LISTENING, user_text="", reply_text="", detail="")
                self.cloud.warm()
                ignore_until = 0.0
                if chime:
                    self.mixer.play(ArraySource(self.sounds.wake, gain=self.cfg.sounds.volume))
                    ignore_until = time.monotonic() + len(self.sounds.wake) / HW_RATE + 0.1
                utt = record_utterance(self.cfg, self.audio, cancel, preroll, ignore_until,
                                       self.cfg.assistant.follow_up_seconds if follow_up else None,
                                       transcribe=self.cloud.transcribe)
                if utt is None:
                    self._ui(cancel, state=State.IDLE)
                    return
                t_end = utt.ended_at
                early = utt.text is not None
                if not early:
                    self._ui(cancel, state=State.TRANSCRIBING)
                    utt.text = self.cloud.transcribe(utt.audio)
                text = utt.text
                log.info("heard %r (%.1fs of audio; transcript ready %.2fs after you stopped, %s)", text,
                         len(utt.audio) / 16000, time.monotonic() - t_end,
                         "transcribed during the pause" if early else "transcribed after it")
                if not text:
                    self._ui(cancel, state=State.IDLE)
                    return
            if cancel.is_set():
                return

            self._ui(cancel, user_text=text, reply_text="")
            norm = normalize(text, name)
            speech = self.speech = SpeechSession(self.cloud, self.mixer, self.cfg, cancel, cache=self.tts_cache,
                                             on_caption=self._captioner(cancel),
                                                 on_audio=lambda: self._first_audio(cancel, t_end))
            if answer_for is not None:
                self._answer_job(cancel, speech, answer_for, text, norm)
                return
            if self.spotify is not None and self.spotify.playing and norm in MUSIC_STOP:
                self._quiet_music()          # "Franklin, stop" with music on means the music
                speech.finish(wait=False)
                self._ui(cancel, state=State.IDLE)
                return
            if norm in STOP_PHRASES or (norm in WHISPER_GHOSTS and not self.status.agent_mode):
                speech.finish(wait=False)
                self._ui(cancel, state=State.IDLE)
                return
            if self.held and READ_HELD.fullmatch(norm):
                speech.finish(wait=False)
                self._ui(cancel, state=State.IDLE)
                self.release_held()
                return

            local = self.commands.handle(norm)      # even on a direct line: Hermes can't set this timer
            if local is not None:
                log.info("answered on the device: %r", local.text)
                if local.text:
                    speech.say(local.text, cache=local.cache)
                elif local.then is None:            # with a `then` (muting), that makes its own sound
                    self._beep(self.sounds.tick)
                speech.finish(wait=True)
                self.assistant.remember(text, local.text)
                self._ui(cancel, state=State.IDLE)
                if local.then is not None and not cancel.is_set():
                    local.then()
                return

            agent = None if self.status.agent_mode else self.assistant.route_personal(norm)
            if agent is not None:           # your email, calendar, files...: no fast-model round, straight to Hermes
                log.info("routed straight to %s: %r", agent.name, text)
                self.agents.start(agent, self.assistant.agent_request(text), announce=True)
                said = handoff_phrase()
                speech.say(said, cache=True)
                speech.finish(wait=True)
                self.assistant.remember(text, said)
                self._ui(cancel, state=State.IDLE)
                return
            if self.status.agent_mode:
                reply = self._agent_turn(cancel, speech, text, norm)
            else:
                self._ui(cancel, state=State.THINKING, detail="")
                reply = self.assistant.respond(text, speech, cancel,
                                               on_text=lambda t: self._ui(cancel, reply_text=t))
                speech.finish(wait=True)
                if speech.error and not cancel.is_set():
                    raise speech.error
                if not reply and not cancel.is_set() and time.monotonic() - self._mic_muted_at > 3:
                    self._beep(self.sounds.tick)    # e.g. "set the volume to 4": done, nothing to say
            if cancel.is_set():
                return
            if self.audio.echo is not None:
                log.info("%s", self.audio.echo.summary())     # how well it could have heard "Franklin" just now
            if self.wake is not None and self.wake.behind_s > 0.3:
                log.warning("wakeword: running %.1f s behind the mic (skipped ahead %d times)",
                            self.wake.behind_s, self.wake.skips)
            self._ui(cancel, state=State.IDLE)
            if self.cfg.assistant.follow_up and (self.assistant.force_follow_up or self.status.agent_mode
                                                 or reply.rstrip().endswith("?")):
                self.assistant.force_follow_up = False
                self.events.put(("followup",))
        except CloudError as e:
            self._error(cancel, str(e))
            if str(e) == "No connection":
                self.status.set(online=False)
        except Exception as e:
            log.exception("turn failed")
            self._error(cancel, "Something went wrong" if not str(e) else str(e)[:60])

    def _first_audio(self, cancel, t_end) -> None:
        if not cancel.is_set() and self.status.state != State.SPEAKING:
            log.info("latency: first audio %.2fs after you stopped talking", time.monotonic() - t_end)
            self.status.set(state=State.SPEAKING)

    def _answer_job(self, cancel, speech, task, text, norm) -> None:
        """Your reply to a background job's question (or to an external agent's result that ended in one)."""
        self._ui(cancel, state=State.THINKING, detail="")
        if DROP_JOB.fullmatch(norm):
            task.cancel.set()
            speech.say("Okay, I'll drop it.", cache=True)
        elif task.status == "waiting":
            self.agents.answer(text, task)
            speech.say(GOT_IT, cache=True)
        else:
            agent = self.agents.get(task.agent) or self.agents.default()
            self.agents.start(agent, f'Earlier the user asked: "{task.request}". You answered: "{task.result}". '
                                     f'The user replied: "{text}"')
            speech.say(handoff_phrase(), cache=True)
        speech.finish(wait=True)
        self.assistant.remember(text, "")
        self._ui(cancel, state=State.IDLE)

    def _agent_turn(self, cancel, speech, text, norm) -> str:
        name = self.status.agent_mode
        if EXIT_AGENT.search(norm):
            self._ui(cancel, agent_mode="", state=State.THINKING)
            speech.say(f"Okay, back to {self.cfg.assistant.name}.", cache=True)
            speech.finish(wait=True)
            return ""
        agent = self.agents.get(name)
        if agent is None:
            self.status.set(agent_mode="")
            raise RuntimeError(f"{name} isn't configured")
        self._ui(cancel, state=State.THINKING, detail=f"Asking {agent.name}")
        splitter, spoken = SentenceSplitter(), []

        def on_text(delta):
            for s in splitter.feed(delta):
                speech.say(s)
                spoken.append(s)
                self._ui(cancel, reply_text=" ".join(spoken))

        def on_progress(p):
            self._ui(cancel, state=State.WORKING, detail=f"{agent.name}: {p}")

        result = agent.run(text, cancel, on_progress, on_text, keep_history=True)
        for s in splitter.flush():
            speech.say(s)
            spoken.append(s)
        if not spoken and result:
            speech.say(result[:700])
            self._ui(cancel, reply_text=result[:700])
        speech.finish(wait=True)
        self.agent_mode_last = time.time()
        return " ".join(spoken) or result

    def _announce(self, cancel, kind, task) -> None:
        """Read out a background job's question or result, with the voice tinted to match."""
        if kind == "joined":
            self._joined(cancel, *task)
            return
        if kind == "question" and task.status != "waiting":
            return                                       # answered (or dropped) in the meantime
        tint = "waiting" if kind == "question" else ("failed" if task.status == "failed" else "")
        self._ui(cancel, state=State.THINKING, detail="", tint=tint, user_text=task.request[:80], reply_text="")
        speech = self.speech = SpeechSession(self.cloud, self.mixer, self.cfg, cancel, cache=self.tts_cache,
                                             on_caption=self._captioner(cancel),
                                             on_audio=lambda: self._ui(cancel, state=State.SPEAKING))
        try:
            if kind == "question":
                text = task.question
            elif task.status == "failed":
                text = "Sorry, I couldn't finish that."
            else:
                text = task.result if task.builtin else \
                    self.assistant.summarize_for_speech(task.agent, task.request, task.result)
            if kind == "result" and time.time() - task.started > 8:
                speech.play(self.sounds.agent_done * self.cfg.sounds.volume)   # chime for news you weren't waiting on
            speech.say(text)
            self._ui(cancel, reply_text=text)
            speech.finish(wait=True)
            if kind == "result":
                self.assistant.note(text)
        finally:
            self.status.set(tint="")
            if kind == "result":       # read out (or interrupted): a failure pulses red a few more seconds
                self._task_news = (tint, time.monotonic() + NEWS_S) if tint else ("", 0.0)
        self._ui(cancel, state=State.IDLE)
        if cancel.is_set():
            return
        if kind == "question" or (not task.builtin and task.status == "done" and text.rstrip().endswith("?")):
            self.events.put(("followup", task))           # your next words go to the job

    def _joined(self, cancel, ssid: str, location: str) -> None:
        """Just got onto a new network from the setup page: say so, and apply the location typed there."""
        text = f"Connected to {ssid}."
        if location and location.lower() != self.cfg.assistant.location.lower():
            try:
                text += " " + settings.set_location(self.cfg, location, self.weather)
            except Exception as e:
                log.warning("couldn't set the location to %r: %s", location, e)
                text += f" I couldn't find {location}, so tell me where I am."
        speech = self.speech = SpeechSession(self.cloud, self.mixer, self.cfg, cancel, cache=self.tts_cache,
                                             on_caption=self._captioner(cancel),
                                             on_audio=lambda: self._ui(cancel, state=State.SPEAKING))
        self._ui(cancel, state=State.THINKING, user_text="", reply_text=text)
        speech.say(text)
        speech.finish(wait=True)
        self._ui(cancel, state=State.IDLE)

    def _captioner(self, cancel):
        """For a new reply: clear the old caption, then show each sentence as it's heard."""
        self._ui(cancel, caption="")
        return lambda text: self._ui(cancel, caption=text, caption_at=time.monotonic())

    def _say_cached(self, text: str) -> None:
        """Speak a phrase without waiting (used offline, so it has to be in the TTS cache)."""
        s = SpeechSession(self.cloud, self.mixer, self.cfg, threading.Event(), cache=self.tts_cache)
        s.say(text, cache=True)
        s.finish(wait=False)

    def _error(self, cancel, msg: str) -> None:
        if cancel.is_set():
            return
        log.error("error: %s", msg)
        self.status.set(state=State.ERROR, detail=msg)
        self.mixer.play(ArraySource(self.sounds.error, gain=self.cfg.sounds.volume))
        time.sleep(3)
        self._ui(cancel, state=State.IDLE, detail="")

    # ── hooks used by the assistant's tools ─────────────────────────────────
    def set_screen(self, level: int) -> None:
        self.status.set(screen=max(0, min(10, int(level))))
        self._save_settings()

    def screen(self) -> int:
        return self.status.screen

    def good_night(self) -> None:
        """Night mode now, easing out from [night] sunrise_start to sunrise_end."""
        until = next_at(self.cfg.night.sunrise_end, dt.datetime.now())
        self.status.set(night_until=until.timestamp(), day_until=0.0)
        self._save_settings()
        log.info("night mode until %s", until)

    def good_morning(self) -> None:
        """No night mode until it would next start by itself (or for 18 hours)."""
        now = dt.datetime.now()
        until = next_at(self.cfg.night.start, now) if self.cfg.night.start else now + dt.timedelta(hours=18)
        self.status.set(night_until=0.0, day_until=until.timestamp())
        self._save_settings()

    def set_local(self, on: bool) -> str:
        """Answers from the model on your computer ([local_llm]) or from the cloud. Returns what to say."""
        L = self.cfg.local_llm
        if on and not (L.url and L.model):
            return "There's no local model set up yet. Run tesseract local on me with your computer's address."
        L.enabled = on
        try:
            settings.set_local_value(self.cfg, "local_llm", "enabled", on)
        except OSError:
            log.exception("couldn't save local mode")
        return "Okay, I'll use the model on your computer." if on else "Okay, back to the cloud."

    def power_off(self, restart: bool = False) -> None:
        """Shut the Pi down (or restart it). The screen says so until the service stops and blanks it,
        a few seconds before the Pi halts."""
        if self.spotify is not None and self.spotify.playing:
            self._quiet_music()
        self.status.set(power="restart" if restart else "off", state=State.IDLE)
        log.info("%s", "restarting" if restart else "shutting down")
        if self.sim:                      # never the laptop
            threading.Timer(3, lambda: self.status.set(power="")).start()
            return
        subprocess.run(["sync"], check=False)
        subprocess.run(["systemctl", "reboot" if restart else "poweroff"], check=False)

    def set_quiet(self, on: bool) -> None:
        self.status.set(quiet=on)
        self._save_settings()
        if not on:
            self.release_held()

    def held_count(self) -> int:
        return len(self.held)

    def release_held(self) -> None:
        """Read out what quiet mode held back (when the speaker is free)."""
        self.announcements.extend(self.held)
        self.held.clear()

    def mute_mic(self) -> None:
        self._mic_muted_at = time.monotonic()
        if not self.status.muted:
            self.events.put(("mute",))

    def mute_sound(self, on: bool) -> bool:
        """Volume to 0, or back to where it was. Returns False if there was nothing to undo."""
        if on:
            self._volume_before_mute = self.status.volume or self._volume_before_mute
            self.set_volume(0)
            return True
        if self.status.volume:
            return False
        self.set_volume(self._volume_before_mute or self.cfg.audio.volume)
        return True

    def set_volume(self, level: int) -> None:
        self.status.set(volume=max(0, min(10, int(level))), volume_changed=time.monotonic())
        self._save_settings()

    def volume(self) -> int:
        return self.status.volume

    def set_location(self, place: str) -> str:
        try:
            return settings.set_location(self.cfg, place, self.weather)
        except LookupError:
            return f"I couldn't find a place called {place}."

    def start_wifi_setup(self) -> None:
        self.events.put(("wifi_setup",))

    def enter_agent_mode(self, name: str) -> None:
        self.agent_mode_last = time.time()
        self.status.set(agent_mode=name)

    def set_working(self, label: str) -> None:
        if threading.current_thread() is not self.turn or self.cancel.is_set():
            return
        if label:
            self.status.set(state=State.WORKING, detail=label)
        elif self.status.state == State.WORKING:
            self.status.set(state=State.THINKING, detail="")

    # ── helpers ─────────────────────────────────────────────────────────────
    def _fetch_vad(self) -> None:
        from .vad import ensure_silero
        for _ in range(30):                   # until there's a network
            try:
                ensure_silero(self.cfg.path("models"))
                return
            except Exception as e:
                log.debug("vad: model not fetched yet (%s)", e)
                time.sleep(20)

    def _duck_music(self) -> bool:
        """Music goes quiet while you talk to Franklin, and under its replies and chimes."""
        return self.status.state != State.IDLE or self.mixer.busy

    def _quiet_music(self) -> None:
        try:
            self.spotify.pause()
        except Exception as e:
            log.warning("spotify: couldn't pause: %s", e)

    def _wake_boost(self) -> float:
        st = self.status.state
        if st in (State.SPEAKING, State.ALARM) or self.mixer.busy:
            return self.cfg.wake.speaking_threshold_boost
        return 0.0

    def _own_voice(self) -> bool:
        """A wake while the speaker is playing (a reply, music) that only its own sound can explain. A
        person saying "Franklin" over it adds sound the echo cancelling can't account for; the leftover of
        its own voice tripping the wake word on a word like "I'm" doesn't (checked on a real recording)."""
        echo = self.audio.echo
        if echo is None or not echo.playing_recently():
            return False
        at = self.wake.wake_position if self.wake is not None else None
        return echo.someone_talked(at=at) is False

    def _hearing_itself(self) -> bool:
        """The reply says "Franklin" and that's what the wake word heard. Before, any reply containing the
        name couldn't be interrupted by voice at all, however long (Hermes's answers often say it). Now a
        person talking over it still counts, judged the same way as _own_voice."""
        # the wake word fires a moment after the word ends, so allow a little time after playback
        s = self.speech
        if s is None or not s.says_wake_word or time.monotonic() - s.finished_at >= 1.5:
            return False
        echo = self.audio.echo
        if echo is not None and echo.playing_recently():
            at = self.wake.wake_position if self.wake is not None else None
            return echo.someone_talked(at=at) is not True
        return True

    def _beep(self, samples) -> None:
        self.mixer.play(ArraySource(samples, gain=self.cfg.sounds.volume))

    def _stop_alarm(self) -> None:
        if self.alarm_source is not None:
            self.alarm_source.stop()
            self.alarm_source = None
        if self.status.state == State.ALARM:
            self.status.set(state=State.IDLE, detail="")

    def _refresh_info(self) -> None:
        """Recompute what the display and LEDs show about alarms, timers and background jobs."""
        live = self.agents.running()
        news, until = self._task_news
        if time.monotonic() > until:
            news = ""
        working = [t for t in live if t.status == "running"]
        if any(t.status == "waiting" for t in live):
            state = "waiting"
        else:
            state = news or ("running" if working else "")
        now = time.time()
        info = dict(next_alarm=self.alarms.next_alarm_text(self.cfg.display.clock_24h),
                    timers=[(t.label, t.due, t.total or max(1.0, t.due - now)) for t in self.alarms.timers()],
                    task_state=state, task_progress=round(max((t.fraction for t in working), default=0.0), 3),
                    task_label=working[-1].progress if working else "")
        if any(getattr(self.status, k) != v for k, v in info.items()):   # only bump the version on real changes
            self.status.set(**info)

    def _connectivity(self) -> None:
        warmed = False
        offline_since, next_fix, fixes = 0.0, 0.0, 0
        while True:
            try:
                socket.create_connection(("openrouter.ai", 443), timeout=4).close()
                online = True
            except OSError:
                online = False
            now = time.monotonic()
            if online:
                if fixes:
                    log.info("network: back after %d fix attempt(s)", fixes)
                offline_since, fixes = 0.0, 0
            elif not self.sim and settings.Wifi.available() and not (self.portal and self.portal.active):
                # Wi-Fi "connected" but nothing gets through doesn't fix itself: log why, then try a fix,
                # 2 min in and then less and less often (a router that's really down gets left alone)
                offline_since = offline_since or now
                if now - offline_since > OFFLINE_FIX_AFTER_S and now >= next_fix:
                    settings.heal_network(fixes)
                    next_fix = now + min(OFFLINE_FIX_AFTER_S * 2 ** fixes, 900)
                    fixes += 1
            if online and not warmed and self.cfg.openrouter_key:
                # the fixed phrases, once there's a network (at boot, Wi-Fi often comes up after we do)
                self.tts_cache.warm(self.cloud, WARM_PHRASES)
                warmed = True
            if online != self.status.online:
                log.info("network: %s", "online" if online else "OFFLINE")
                self.status.set(online=online)
            self._refresh_info()   # also keeps the "next alarm" text current
            time.sleep(30 if online else 10)

    def _keyboard(self) -> None:
        print("[sim] type a request and press Enter, or a key: a=action h=hold +/-=volume m=mute",
              file=sys.stderr)
        keys = {"a": ("action",), "h": ("action_hold",), "+": ("volume", 1), "=": ("volume", 1),
                "-": ("volume", -1), "m": ("mute",)}
        try:
            for line in sys.stdin:
                line = line.strip()
                if line in keys:
                    self.events.put(keys[line])
                elif line:
                    self.events.put(("text", line))
        except (OSError, ValueError):
            pass  # no usable terminal (e.g. under a test runner)

    def _apply_saved(self) -> None:
        """Volume, mute, screen brightness and night mode, as they were last left."""
        saved = self._load_settings()      # (not `settings`: that's the module, used for Wi-Fi)
        self.status.set(volume=int(saved.get("volume", self.cfg.audio.volume)),
                        muted=bool(saved.get("muted", False)), quiet=bool(saved.get("quiet", False)),
                        screen=int(saved.get("screen", self.cfg.display.brightness)),
                        night_until=float(saved.get("night_until", 0.0)),
                        day_until=float(saved.get("day_until", 0.0)))
        if hasattr(self, "audio"):
            self.audio.muted = self.status.muted

    def _load_settings(self) -> dict:
        try:
            return json.loads(self._settings_path.read_text())
        except Exception:
            return {}

    def _save_settings(self) -> None:
        try:
            s = self.status
            self._settings_path.write_text(json.dumps({"volume": s.volume, "muted": s.muted, "quiet": s.quiet,
                                                       "screen": s.screen,
                                                       "night_until": s.night_until, "day_until": s.day_until}))
        except OSError:
            log.exception("couldn't save settings")
