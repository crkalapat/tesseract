"""The fast model: the front desk you talk to.

One streamed LLM call both answers and routes. Its tool choice *is* the router,
so a simple question costs no extra classification step:

    "what's 15% of 80?"            -> answers itself
    "what's the weather tomorrow?" -> get_weather (Open-Meteo, ~150 ms) -> phrases it
    "who won the game last night?" -> web_search, and the search answer is spoken directly
    "set a timer for 10 minutes"   -> set_timer, confirmation spoken directly
    "add the robotics club meeting on Friday at 6 to my calendar"
                                   -> start_task: "On it." now; the thinking agent does it
                                      in the background and the result is announced later

Tools whose result is already the right thing to say (SPOKEN) end the turn
right there instead of going back to the model for phrasing, which saves a
whole LLM round trip on timers, searches and hand-offs.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .commands import QUIET_OFF, QUIET_ON, SOUND_OFF
from .speech import SPOKEN_STYLE, SentenceSplitter
from .util import format_clock, spoken_duration

log = logging.getLogger(__name__)

# said when a background job starts. Calm, with periods: an exclamation mark makes the voice chirp, which
# gets uncanny fast. Picked at random, never the same twice in a row; all cached, so they play instantly.
HANDOFF = ["On it.", "Give me one second.", "Let me go do that real quick.", "Okay, I'm on it.",
           "Sure, one moment.", "Alright, I'll get back to you.", "Okay, give me a minute."]
_last_handoff = ""


def handoff_phrase() -> str:
    global _last_handoff
    _last_handoff = random.choice([p for p in HANDOFF if p != _last_handoff])
    return _last_handoff


ONE_SEC = "One sec."          # said the moment a web search starts (searches take ~3 s)
GOT_IT = "Got it."
FAILED = "Sorry, that didn't work."
# results that are spoken as-is (no second LLM round); CACHED ones have fixed wording
SPOKEN = {"set_timer", "set_alarm", "list_timers_and_alarms", "cancel_timers_or_alarms", "set_volume", "web_search",
          "start_task", "answer_task", "task_status", "cancel_task", "talk_to_agent", "set_location",
          "set_screen_brightness", "night_mode", "play_music", "music_control", "mute", "quiet_mode",
          "read_waiting_results"}
CACHED = {"set_timer", "set_alarm", "cancel_timers_or_alarms", "start_task", "answer_task", "cancel_task", "night_mode",
          "mute", "quiet_mode"}
# the user's own stuff: goes straight to their agent (Hermes), with no fast-model round that could only
# say "I can't see your email". Device things (timers, alarms) stay here.
PERSONAL = re.compile(r"\b(e ?mails?|inbox|gmail|calendars?|agenda|appointments?|meetings?|spreadsheets?|budget|"
                      r"google (drive|docs?|sheets?|calendar)|(to ?do|todo) list|"
                      r"my ((recent|last|latest|upcoming|next|current|new|old|unread|monthly) )?(drive|docs?|documents?|files?|folders?|notes?|sheets?|schedule|week|weekend|day|events?|"
                      r"tasks?|to ?dos?|todos?|contacts?|messages?|accounts?|reminders?|spending|purchases?|orders?|"
                      r"receipts?|transactions?|bank|balance|bills?|subscriptions?|money|expenses?)|"
                      r"(did|have) i (spend|spent|pay|paid|buy|bought|order|ordered)|how much (money )?(did|have) i)\b")
# the fast model saying it's on it without calling start_task (it happened: "On it. On it.", then nothing)
EMPTY_ACK = re.compile(r"(okay|ok|sure|alright|got it)?[,. ]*(i'?m )?(on it|working on it|on that|let me (check|look|see|find|"
                       r"go)|give me (a|one) (sec|second|moment|minute)|one (sec|second|moment)|just a (sec|second|moment)|"
                       r"i'?ll (check|look|find out|get back to you))\b[^?]*", re.I)
NOT_PERSONAL = re.compile(r"\b(timers?|alarms?|wake me|volume|play|playlist|spotify)\b")
SLOW = {"web_search": "Searching the web", "get_weather": "Checking the weather"}
SEARCH_PROMPT = ("Answer the question from current web results in one or two short spoken sentences. Lead with the "
                 "answer. Plain text only, no lists or markdown. " + SPOKEN_STYLE)


def _fn(name, desc, props=None, required=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": list(required)}}}


class Assistant:
    def __init__(self, cfg, cloud, alarms, agents, hooks, weather=None):
        """hooks: object with set_volume(int) -> None, volume() -> int, enter_agent_mode(name) -> None,
        set_working(label) -> None, set_location(place) -> str (what to say), set_screen(int),
        screen() -> int, good_night(), good_morning()"""
        self.cfg, self.cloud, self.alarms, self.agents, self.hooks = cfg, cloud, alarms, agents, hooks
        self.weather = weather
        self.history: list[dict] = []
        self.last_turn = 0.0
        self.force_follow_up = False
        self._cancel = threading.Event()

    def forget(self) -> None:
        self.history.clear()

    def remember(self, user_text: str, reply: str) -> None:
        self.history += [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply or "(done)"}]
        self.history = self.history[-2 * self.cfg.assistant.max_history_turns:]
        self.last_turn = time.time()

    def note(self, text: str) -> None:
        """Something the user heard outside a turn (a finished background job), for follow-ups."""
        if time.time() - self.last_turn > self.cfg.assistant.memory_minutes * 60:
            self.history.clear()
        self.history.append({"role": "assistant", "content": text})
        self.last_turn = time.time()

    # ── straight to the agent ───────────────────────────────────────────────
    def route_personal(self, norm: str):
        """The external agent (Hermes) for anything about the user's own stuff, else None."""
        ext = self.agents.external()
        if ext and PERSONAL.search(norm) and not NOT_PERSONAL.search(norm):
            return ext[0]
        return None

    def agent_request(self, text: str) -> str:
        """What the agent gets: your words, plus the last exchange if it was just now ('add that to my calendar')."""
        if not self.history or time.time() - self.last_turn > 300:
            return text
        # not the assistant's name: an agent that repeats it can trip the wake word while it's read out
        ctx = " ".join(f"{'User' if m['role'] == 'user' else 'Speaker'}: {m['content']}" for m in self.history[-4:])
        return f"{text}\n\n(For context, what the user said to the speaker just before this: {ctx[:800]})"

    # ── prompt ──────────────────────────────────────────────────────────────
    def system_prompt(self) -> str:
        a = self.cfg.assistant
        agents = list(self.agents.agents.values())
        if len(agents) == 1 and agents[0].builtin:
            jobs = "their to-do list, calendar or notes"
        else:
            jobs = "their calendar, to-do list, smart home or other accounts"
        lines = [
            f"You are {a.name}, the voice assistant inside Tesseract: a small cube-shaped smart speaker and alarm "
            "clock. Everything you write is spoken aloud by a text-to-speech voice, so:",
            "- Answer in one or two short sentences unless asked for more. Lead with the answer.",
            "- Plain conversational sentences only: no markdown, lists, emojis or code. " + SPOKEN_STYLE,
            "- The user's words come from speech recognition and may contain errors; interpret them charitably. "
            "Ignore your own name if it appears at the start.",
            "- Prefer a reasonable assumption over a clarifying question.",
            "",
            "Pick the fastest way to help:",
            "- Stable knowledge you are certain of (definitions, math, how things work, well-known history, advice, "
            "chit-chat): answer yourself.",
            "- Every other factual question: web_search. That includes specific people, companies, products, prices, "
            "sports, scores, release dates, rankings, statistics, laws, local places, opening hours, news, anything "
            "from 2024 on, and anything you are not completely sure of. Never guess a name, number or date: search.",
            "- Weather or temperature: get_weather.",
            "- Timers, alarms, volume, muting, quiet mode and screen brightness: their tools. You can do all of these.",
            f"- Anything involving {jobs} (email, documents, spreadsheets, budget, files, notes, schedule), several "
            "steps, or doing something on the user's behalf: start_task. You can't see any of it yourself, so never "
            "answer it or say you can't: hand it off. Put every detail the user gave into the request (names, dates, "
            "times, places). Write no text with start_task; the speaker acknowledges it by itself.",
            "- Never say you're working on something, checking, or getting back to the user unless you call start_task "
            "in the same reply: words alone do nothing.",
            "- Never say you can't do something: if no tool fits and it isn't knowledge you're sure of, use "
            "start_task.",
            "- Never say you did something unless you called the tool for it.",
        ]
        if a.extra_instructions:
            lines += ["", a.extra_instructions]
        # everything that changes goes last, so the start of the prompt stays the same and providers can cache it
        now = dt.datetime.now()
        lines += ["", f"Current local time: {now:%A, %B} {now.day}, {now.year}, {now:%I:%M %p}.".replace(" 0", " ")]
        if a.location:
            lines.append(f"The user is in {a.location}.")
        items = self.alarms.list()
        if items:
            lines.append("Active timers and alarms: " + "; ".join(i.describe(self.cfg.display.clock_24h) for i in items))
        if len(agents) > 1:
            lines.append("Agents start_task can use: " + "; ".join(f"{ag.name}: {ag.description}" for ag in agents))
        running = [t for t in self.agents.running() if t.status == "running"]
        if running:
            lines.append("Background jobs in progress: " + "; ".join(t.request[:60] for t in running))
        for t in self.agents.waiting():
            lines.append(f'A background job is waiting for the user to answer: "{t.question}". If the message '
                         "answers it, call answer_task.")
        return "\n".join(lines)

    # ── tools ───────────────────────────────────────────────────────────────
    def tools(self) -> list[dict]:
        task_props = {"request": {"type": "string", "description": "The full request, self-contained: the user's "
                                  "words plus every detail needed"}}
        names = self.agents.names()
        task_required = ["request"]
        if len(names) > 1:   # required: picking by description beats falling back to whichever is first
            task_props["agent"] = {"type": "string", "enum": names}
            task_required.append("agent")
        t = [
            _fn("set_timer", "Start a countdown timer.",
                {"seconds": {"type": "integer", "description": "Duration in seconds"},
                 "label": {"type": "string", "description": "Optional short name, e.g. 'pasta'"}}, ["seconds"]),
            _fn("set_alarm", "Set an alarm for a time of day.",
                {"time": {"type": "string", "description": "24-hour HH:MM local time"},
                 "label": {"type": "string"},
                 "repeat": {"type": "string", "enum": ["once", "daily", "weekdays", "weekends"]}}, ["time"]),
            _fn("list_timers_and_alarms", "Say which timers and alarms are set."),
            _fn("cancel_timers_or_alarms", "Cancel timers/alarms.",
                {"which": {"type": "string", "description": "A label, 'timers', 'alarms', or 'all'"}}, ["which"]),
            _fn("set_volume", "Set the speaker volume.",
                {"level": {"type": "integer", "description": "0 (silent) to 10 (max)"}}, ["level"]),
            _fn("set_screen_brightness", "Set the display's brightness ('it's too bright', 'turn the screen off').",
                {"level": {"type": "integer", "description": "0 (off) to 10 (max); now " +
                           str(self.hooks.screen() if hasattr(self.hooks, "screen") else 10)}}, ["level"]),
            _fn("night_mode", "The user is going to sleep (on: dim, warm screen, LEDs off, brightening again from "
                "6:30 to 7:30) or is up for the day (off).", {"on": {"type": "boolean"}}, ["on"]),
            _fn("get_weather", "Current weather and the forecast for the next few days.",
                {"location": {"type": "string", "description": "City, if the user named one; empty for home"}}),
            _fn("web_search", "Search the web for current information; the answer is read to the user directly.",
                {"query": {"type": "string", "description": "A complete, self-contained question"}}, ["query"]),
            _fn("start_task", "Hand a multi-step job to the background agent (calendar, to-do list, notes, research, "
                "anything on the user's behalf). The result is announced when it's done.", task_props, task_required),
            _fn("set_location", "Save where the user is (used for the weather, local searches and the clock's "
                "time zone) whenever they tell you: 'I'm in Chicago', 'set my location to Austin', or a city given "
                "in answer to you asking where they are. Not for asking about another city.",
                {"place": {"type": "string", "description": "City, State"}}, ["place"]),
            _fn("mute", "Mute the microphone ('mute', 'stop listening'; the user turns it back on with the mute "
                "button) or the sound (the speaker).", {"what": {"type": "string", "enum": ["microphone", "sound"]}},
                ["what"]),
            _fn("quiet_mode", "Quiet mode on or off. On: finished background jobs aren't read out; a green light "
                "shows a result is ready, and it's read when the user asks or taps the button.",
                {"on": {"type": "boolean"}}, ["on"]),
            _fn("task_status", "Say how the background jobs are going."),
            _fn("cancel_task", "Stop a running background job.", {"agent": {"type": "string"}}),
        ]
        if getattr(self.hooks, "spotify", None) is not None:
            t.append(_fn("play_music", "Play music on Spotify on this speaker: a song, an artist, an album, a "
                         "playlist, the user's liked songs, or music for a mood, genre or activity (as a playlist).",
                         {"query": {"type": "string", "description": "Song, artist, album or playlist name, or a "
                                    "mood/genre like 'chill study music'"},
                          "kind": {"type": "string", "enum": ["track", "artist", "album", "playlist", "liked"]},
                          "artist": {"type": "string", "description": "For a song or album: who it's by"}},
                         ["query", "kind"]))
            t.append(_fn("music_control", "Control the music playing on Spotify.",
                         {"action": {"type": "string", "enum": ["pause", "resume", "next", "previous", "like",
                                                                "shuffle_on", "shuffle_off", "whats_playing"]}},
                         ["action"]))
        if getattr(self.hooks, "held_count", lambda: 0)():
            t.append(_fn("read_waiting_results", "Read out the background job results quiet mode is holding "
                         "('what did you find?', 'what's the answer?')."))
        if self.agents.waiting():
            t.append(_fn("answer_task", "Give the user's answer to the waiting background job.",
                         {"answer": {"type": "string"}}, ["answer"]))
        ext = [a.name for a in self.agents.external()]
        if ext:
            t.append(_fn("talk_to_agent", "Switch to a direct voice conversation with an agent: everything the user "
                         "says next goes straight to it until they ask to come back.",
                         {"agent": {"type": "string", "enum": ext}}, ["agent"]))
        return t

    def run_tool(self, name: str, args: dict, say=None) -> str:
        """The result: the sentence to speak for SPOKEN tools, data for the model otherwise.
        say: web_search streams its answer through it sentence by sentence."""
        h24 = self.cfg.display.clock_24h
        try:
            if name == "set_timer":
                secs, label = int(args["seconds"]), (args.get("label") or "").strip()
                self.alarms.add_timer(secs, label)
                dur = spoken_duration(secs)
                return f"{label.capitalize()} timer set for {dur}." if label else f"Timer set for {dur}."
            if name == "set_alarm":
                it = self.alarms.add_alarm(args["time"], args.get("label", ""), args.get("repeat", "once"))
                when = dt.datetime.fromtimestamp(it.due)
                hhmm, ampm = format_clock(when, h24)
                day = {0: "", 1: " tomorrow"}.get((when.date() - dt.date.today()).days, f" on {when:%A}")
                rep = "" if it.repeat == "once" else f", {it.repeat}"
                return f"Alarm set for {hhmm} {ampm}{day}{rep}.".replace(" .", ".").replace("  ", " ")
            if name == "list_timers_and_alarms":
                items = self.alarms.list()
                return ("; ".join(i.describe(h24) for i in items) + ".") if items else "Nothing's set."
            if name == "cancel_timers_or_alarms":
                gone = self.alarms.cancel(args.get("which", "all"))
                return (f"Cancelled {len(gone)}." if len(gone) > 1 else "Cancelled.") if gone else "Nothing matched."
            if name == "set_volume":
                self.hooks.set_volume(int(args["level"]))
                return ""
            if name == "get_weather":
                if self.weather is None:
                    return "Weather isn't available."
                return self.weather.report(args.get("location", ""))
            if name == "web_search":
                return self._search(args["query"], say)
            if name == "start_task":
                agent = self.agents.get(args.get("agent", "")) or self.agents.default()
                if agent is None:
                    return "There's no agent set up for that."
                self.agents.start(agent, args["request"], announce=True)
                return handoff_phrase()
            if name == "answer_task":
                return GOT_IT if self.agents.answer(args["answer"]) else "Nothing is waiting for an answer."
            if name == "task_status":
                return self.agents.spoken_status()
            if name == "set_location":
                return self.hooks.set_location(args["place"])
            if name == "set_screen_brightness":
                self.hooks.set_screen(int(args["level"]))
                return ""
            if name == "night_mode":
                (self.hooks.good_night if args.get("on", True) else self.hooks.good_morning)()
                return "Good night." if args.get("on", True) else "Good morning."
            if name in ("play_music", "music_control"):
                return self._music(name, args)
            if name == "cancel_task":
                hit = self.agents.cancel(args.get("agent", ""))
                return "Okay, I stopped it." if hit else "Nothing was running."
            if name == "mute":
                if args.get("what") == "sound":     # after this sentence, or it would be said at volume 0
                    threading.Timer(2.0, self.hooks.mute_sound, args=(True,)).start()
                    return SOUND_OFF
                self.hooks.mute_mic()
                return ""                           # no words: the mute sound says it
            if name == "quiet_mode":
                on = bool(args.get("on", True))
                self.hooks.set_quiet(on)
                return QUIET_ON if on else QUIET_OFF
            if name == "read_waiting_results":
                self.hooks.release_held()
                return ""
            if name == "talk_to_agent":
                agent = self.agents.get(args.get("agent", ""))
                if not agent or agent.builtin:
                    return f"There's no agent called {args.get('agent')}."
                self.hooks.enter_agent_mode(agent.name)
                self.force_follow_up = True
                return f"Okay, you're talking to {agent.name}. Say back to {self.cfg.assistant.name} when you're done."
            return f"Unknown tool {name}."
        except Exception as e:
            log.exception("tool %s failed", name)
            return FAILED if name in SPOKEN else f"The {name} tool failed: {e}"

    def _music(self, name: str, args: dict) -> str:
        from .spotify import SpotifyError
        sp = self.hooks.spotify
        try:
            if name == "play_music":
                return sp.play(args.get("query", ""), args.get("kind", ""), args.get("artist", ""))
            action = args.get("action", "")
            if action.startswith("shuffle"):
                return sp.shuffle(action == "shuffle_on")
            return {"pause": sp.pause, "resume": sp.resume, "next": sp.next, "previous": sp.previous,
                    "like": sp.like, "whats_playing": sp.whats_playing}[action]()
        except SpotifyError as e:
            return str(e)

    def _search(self, query: str, say=None) -> str:
        loc = self.cfg.assistant.location or "unknown"
        msgs = [{"role": "system", "content": SEARCH_PROMPT},
                {"role": "user", "content": f"{query}\n(User location: {loc}. Today: {dt.date.today():%A, %B %d, %Y}.)"}]
        plugins = [{"id": "web", "max_results": 3}]
        if say is None:
            return self.cloud.chat(msgs, model=self.cfg.llm.search_model, plugins=plugins)
        splitter, out = SentenceSplitter(), []
        for kind, val in self.cloud.chat_stream(msgs, cancel=self._cancel, model=self.cfg.llm.search_model,
                                                plugins=plugins):
            if kind == "text":
                for s in splitter.feed(val):
                    say(s)
                    out.append(s)
        for s in splitter.flush():
            say(s)
            out.append(s)
        return " ".join(out)

    # ── the loop ────────────────────────────────────────────────────────────
    def respond(self, user_text: str, speech, cancel: threading.Event, on_text=lambda t: None) -> str:
        if time.time() - self.last_turn > self.cfg.assistant.memory_minutes * 60:
            self.history.clear()
        self.force_follow_up = False
        self._cancel = cancel
        messages = [{"role": "system", "content": self.system_prompt()}, *self.history,
                    {"role": "user", "content": user_text}]
        tools = self.tools()
        spoken: list[str] = []

        def say(s: str, cache: bool = False) -> None:
            speech.say(s, cache=cache)
            spoken.append(s)
            on_text(" ".join(spoken))

        called = False
        for _round in range(4):
            splitter = SentenceSplitter(first_clause_chars=60 if not spoken else 10_000)
            text, calls = "", []
            t0, first = time.monotonic(), True
            for kind, val in self.cloud.chat_stream(messages, tools, cancel):
                if first:
                    log.info("llm: first %s after %.2fs", "words" if kind == "text" else kind, time.monotonic() - t0)
                    first = False
                if kind == "text":
                    text += val
                    for s in splitter.feed(val):
                        say(s)
                elif kind == "tool_calls":
                    calls = val
            if cancel.is_set():
                return " ".join(spoken)
            for s in splitter.flush():
                say(s)
            if not calls:
                break

            parsed = []
            for c in calls:
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                parsed.append((c, c["function"]["name"], args))
            log.info("tools: %s", ", ".join(f"{n}({json.dumps(a)[:80]})" for _, n, a in parsed))
            called = True

            if all(name in SPOKEN for _, name, _ in parsed):
                # say the results ourselves: no second trip to the model
                for _, name, args in parsed:
                    self.hooks.set_working(SLOW.get(name, ""))
                    if name == "web_search" and not spoken:
                        say(ONE_SEC, cache=True)        # instant, from the cache, so the wait isn't silent
                    out = self.run_tool(name, args, say=say if name == "web_search" else None)
                    if out and name != "web_search":
                        say(out, cache=name in CACHED)
                self.hooks.set_working("")
                break

            messages.append({"role": "assistant", "content": text or None, "tool_calls": calls})
            self.hooks.set_working(SLOW.get(parsed[0][1], "Working"))
            with ThreadPoolExecutor(max_workers=len(parsed)) as pool:
                results = list(pool.map(lambda p: self.run_tool(p[1], p[2]), parsed))
            for (c, name, _), result in zip(parsed, results):
                messages.append({"role": "tool", "tool_call_id": c["id"], "name": name, "content": result})
            self.hooks.set_working("")

        reply = " ".join(spoken)
        if not called and reply and EMPTY_ACK.fullmatch(reply.strip()) and self.agents.default() is not None:
            # it promised to do it and called nothing: do what it said, rather than drop the request
            agent = (self.agents.external() or [self.agents.default()])[0]
            log.warning("llm: said %r but called no tool; starting the job anyway", reply)
            self.agents.start(agent, self.agent_request(user_text), announce=True)
        self.remember(user_text, reply)
        return reply

    def summarize_for_speech(self, agent: str, request: str, result: str) -> str:
        """An external agent's result, cut down to something worth hearing."""
        if len(result) < 280:
            return result
        try:
            return self.cloud.chat([
                {"role": "system", "content": "Turn an agent's result into a spoken update of at most two short "
                 "sentences. Lead with the outcome. Plain text only. " + SPOKEN_STYLE},
                {"role": "user", "content": f"Agent: {agent}\nTask: {request}\nResult:\n{result[:6000]}"}])
        except Exception:
            return result[:200]
