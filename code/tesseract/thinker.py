"""The built-in background agent: the [thinking] model working through a job
step by step with tools, while the speaker stays free.

It's used when config.toml lists no [[agents]]. Its tools work on the device
itself: a to-do list, a calendar (with reminders that ring like alarms) and
notes (organizer.py), plus the weather, web search and reading web pages. When
it needs something only you know, it calls ask_user: Franklin asks the question
out loud, the LEDs show it's waiting, and your answer comes back here.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re

import httpx

from .speech import SPOKEN_STYLE

from .agents import Agent
from .organizer import parse_when

log = logging.getLogger(__name__)

# what the LEDs/display say while a tool runs
LABELS = {"web_search": "Searching the web", "read_webpage": "Reading a web page", "get_weather": "Checking the weather",
          "todo_add": "Updating your to-do list", "todo_update": "Updating your to-do list",
          "todo_list": "Checking your to-do list", "calendar_add": "Adding it to your calendar",
          "calendar_remove": "Updating your calendar", "calendar_list": "Checking your calendar",
          "note_add": "Saving a note", "notes_search": "Checking your notes", "ask_user": "Waiting for you"}


def _fn(name, desc, props=None, required=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": list(required)}}}


TOOLS = [
    _fn("web_search", "Search the web; returns a short answer with the key facts.", {"query": {"type": "string"}},
        ["query"]),
    _fn("read_webpage", "Fetch a web page and return its text.", {"url": {"type": "string"}}, ["url"]),
    _fn("get_weather", "Current weather and a few days of forecast.",
        {"location": {"type": "string", "description": "City; empty for the user's home"}}),
    _fn("todo_add", "Add an item to the user's to-do list.",
        {"text": {"type": "string"}, "due": {"type": "string", "description": "YYYY-MM-DD, optional"}}, ["text"]),
    _fn("todo_list", "The user's to-do list (open items, with due dates).",
        {"include_done": {"type": "boolean"}}),
    _fn("todo_update", "Mark a to-do item done, reopen it, or remove it.",
        {"item": {"type": "string", "description": "Its id, or words from its text"},
         "action": {"type": "string", "enum": ["complete", "reopen", "remove"]}}, ["item", "action"]),
    _fn("calendar_add", "Add an event to the user's calendar.",
        {"title": {"type": "string"},
         "start": {"type": "string", "description": "Local time, YYYY-MM-DDTHH:MM (or YYYY-MM-DD for all day)"},
         "end": {"type": "string", "description": "Optional, same format"},
         "location": {"type": "string"}, "details": {"type": "string"},
         "remind_minutes_before": {"type": "integer", "description": "Optional: ring a reminder this long before"}},
        ["title", "start"]),
    _fn("calendar_list", "Events between two dates (inclusive). Defaults to the next 7 days.",
        {"from_date": {"type": "string", "description": "YYYY-MM-DD"}, "to_date": {"type": "string"}}),
    _fn("calendar_remove", "Delete an event.", {"event": {"type": "string", "description": "Its id, or words from its title"}},
        ["event"]),
    _fn("note_add", "Remember a fact or note for the user.", {"text": {"type": "string"}}, ["text"]),
    _fn("notes_search", "Look through the user's saved notes.", {"query": {"type": "string"}}),
    _fn("ask_user", "Ask the user one short question out loud and wait for the spoken answer. Only for things "
        "you can't reasonably assume.", {"question": {"type": "string"}}, ["question"]),
]


def page_text(markup: str, limit: int = 6000) -> str:
    markup = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", markup)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", markup))
    return re.sub(r"\s+", " ", text).strip()[:limit]


class ThinkingAgent(Agent):
    builtin = True

    def __init__(self, cfg, cloud, organizer, alarms, weather):
        self.cfg, self.cloud, self.org, self.alarms, self.weather = cfg, cloud, organizer, alarms, weather
        self.name = cfg.assistant.name
        self.description = ("The built-in agent: manages the user's to-do list, calendar (with reminders) and "
                            "notes on this device, and can research things on the web.")

    def system_prompt(self) -> str:
        a = self.cfg.assistant
        now = dt.datetime.now()
        return "\n".join([
            f"You are the background agent of {a.name}, a voice assistant in a small smart speaker. The user asked "
            "for something that takes a few steps. Do it with your tools, then report back.",
            "- Use the fewest steps that get it right. Never invent facts or say you did something you didn't.",
            "- The request came through speech recognition, so names may be misspelled; interpret charitably.",
            "- Resolve relative dates yourself ('Friday', 'tomorrow', 'next week') from today's date below.",
            "- If something essential is missing (say, an event with no day), call ask_user with one short question. "
            "Don't ask about things you can reasonably assume.",
            "- Finish with your report: one or two short spoken sentences, plain text, no markdown or lists. "
            "Lead with the outcome.",
            "- " + SPOKEN_STYLE,
            "",
            f"Today is {now:%A, %B} {now.day}, {now.year}; the time is {now:%I:%M %p}.".replace(" 0", " "),
            f"The user is in {a.location}." if a.location else "",
        ])

    def run(self, task, cancel, on_progress=lambda *p: None, on_text=None, ask=None, **_) -> str:
        T = self.cfg.thinking
        messages = [{"role": "system", "content": self.system_prompt()}, {"role": "user", "content": task}]
        on_progress("Thinking", 0.1)
        for step in range(T.max_steps):
            if cancel.is_set():
                return "Stopped."
            msg = self.cloud.complete(messages, TOOLS, llm=T)
            calls = msg.get("tool_calls") or []
            if not calls:
                return (msg.get("content") or "").strip() or "Done."
            messages.append({"role": "assistant", "content": msg.get("content") or None, "tool_calls": calls})
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                on_progress(LABELS.get(name, "Working"))
                log.info("thinker: %s(%s)", name, json.dumps(args)[:120])
                result = self.tool(name, args, ask)
                messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "name": name, "content": result})
        return "I couldn't finish that in a reasonable number of steps."

    # ── tools ───────────────────────────────────────────────────────────────
    def tool(self, name: str, a: dict, ask=None) -> str:
        try:
            if name == "web_search":
                return self.cloud.chat(
                    [{"role": "user", "content": f"{a['query']}\n\nAnswer with the key facts, briefly. "
                      f"User location: {self.cfg.assistant.location or 'unknown'}. Today: {dt.date.today():%A %B %d %Y}."}],
                    model=self.cfg.llm.search_model, plugins=[{"id": "web", "max_results": 4}])
            if name == "read_webpage":
                r = httpx.get(a["url"], follow_redirects=True, timeout=10,
                              headers={"User-Agent": "Mozilla/5.0 (Tesseract voice assistant)"})
                r.raise_for_status()
                return page_text(r.text)
            if name == "get_weather":
                return self.weather.report(a.get("location", ""))
            if name == "todo_add":
                it = self.org.todo_add(a["text"], a.get("due", ""))
                return f"Added (id {it['id']})."
            if name == "todo_list":
                items = self.org.todos(bool(a.get("include_done")))
                return json.dumps(items) if items else "The to-do list is empty."
            if name == "todo_update":
                it = self.org.todo_update(a["item"], a.get("action", "complete"))
                return f"{a.get('action', 'complete')}: {it['text']}" if it else "No matching item."
            if name == "calendar_add":
                ev = self.org.event_add(a["title"], a["start"], a.get("end", ""), a.get("location", ""),
                                        a.get("details", ""))
                out = f"Added '{ev['title']}' at {ev['start']} (id {ev['id']})."
                if a.get("remind_minutes_before") is not None:
                    when = parse_when(a["start"]) - dt.timedelta(minutes=int(a["remind_minutes_before"]))
                    if when > dt.datetime.now():
                        self.alarms.add_at(when, ev["title"])
                        out += f" Reminder set for {when:%Y-%m-%d %H:%M}."
                return out
            if name == "calendar_list":
                start = dt.datetime.fromisoformat(a["from_date"]) if a.get("from_date") else \
                    dt.datetime.combine(dt.date.today(), dt.time())
                end = (dt.datetime.fromisoformat(a["to_date"]) + dt.timedelta(days=1)) if a.get("to_date") else \
                    start + dt.timedelta(days=7)
                evs = self.org.events_between(start, end)
                return json.dumps(evs) if evs else "No events in that range."
            if name == "calendar_remove":
                ev = self.org.event_remove(a["event"])
                return f"Removed '{ev['title']}'." if ev else "No matching event."
            if name == "note_add":
                self.org.note_add(a["text"])
                return "Saved."
            if name == "notes_search":
                notes = self.org.notes(a.get("query", ""))
                return json.dumps(notes[-20:]) if notes else "No matching notes."
            if name == "ask_user":
                if ask is None:
                    return "(Can't ask the user right now. Make a reasonable assumption.)"
                return ask(a["question"])
            return f"Unknown tool {name}."
        except Exception as e:
            log.exception("thinker: %s failed", name)
            return f"The {name} tool failed: {e}"
