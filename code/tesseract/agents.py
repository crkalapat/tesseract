"""Background jobs, and the agents that do them.

The fast model hands anything multi-step ("add this event to my calendar",
"what's on my to-do list") to an agent with start_task, says "On it." (or similar), and the
result is announced when it's done. Who does the job:

  * no [[agents]] in config.toml -> the built-in agent (thinker.py: the [thinking] model)
  * exactly one                  -> that one, every time; no need to say its name
  * several                      -> the fast model picks by description, or you name one
  ([thinking] keep_builtin = true adds the built-in agent as one more choice)

External agents: Hermes / OpenClaw / any OpenAI-compatible server, Home
Assistant, a webhook, or a command. "Franklin, let me talk to Hermes" opens a
direct line where every turn goes straight to it until "back to Franklin".
"""

from __future__ import annotations

import json
import logging
import math
import queue
import re
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

import httpx

from .speech import SPOKEN_STYLE

log = logging.getLogger(__name__)

VOICE_NOTE = ("The user is talking to you by voice through a smart speaker called Tesseract. Your reply "
              "will be read aloud: keep it short and conversational, no markdown, lists or code. " + SPOKEN_STYLE +
              " If the user tells you something lasting about themselves (people, preferences, routines, plans) or "
              "asks you to remember something, save it to your memory right away. If they mention something from "
              "an earlier conversation that you can't see here, search your past conversations before saying you "
              "don't know.")


# What a tool call looks like on the screen: Hermes reports raw tool names ("mcp_google_get_events")
STEP_WORDS = [
    (r"gmail|e_?mail|inbox", "Checking email"),
    (r"manage_event|create_event|update_event|delete_event", "Updating calendar"),
    (r"calendar|event|freebusy", "Checking calendar"),
    (r"(modify|update|append|create)_?(sheet|spreadsheet)|modify_sheet", "Updating a sheet"),
    (r"sheet|spreadsheet", "Reading a sheet"),
    (r"(modify|create|insert|batch_update)_?doc", "Editing a doc"),
    (r"doc", "Reading a doc"),
    (r"drive", "Searching Drive"),
    (r"session_search", "Searching past chats"),
    (r"memory", "Checking memory"),
    (r"web_search|search", "Searching the web"),
    (r"web_extract|extract|fetch|read_url|web", "Reading a page"),
    (r"browser", "Using the browser"),
    (r"computer|cua", "Using the computer"),
    (r"vision|image", "Looking at an image"),
    (r"terminal|shell|code_exec", "Running code"),
    (r"todo|plan", "Planning"),
    (r"cron", "Scheduling"),
]


def step_words(tool: str) -> str:
    """'mcp_google_get_events' -> 'Checking your calendar'; unknown tools -> 'Get events'."""
    raw = re.sub(r"^[^\w]+", "", str(tool)).strip()          # Hermes may lead with an emoji
    name = (re.match(r"[\w.-]+", raw) or [raw])[0].lower()
    for pattern, words in STEP_WORDS:
        if re.search(pattern, name):
            return words
    name = re.sub(r"^mcp_[a-z0-9]+_", "", name).replace("_", " ").replace("-", " ").strip()
    return (name[:1].upper() + name[1:])[:24] or "Working"


class Agent:
    builtin = False

    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg.name
        self.description = cfg.description

    def run(self, task: str, cancel: threading.Event, on_progress: Callable[..., None] = lambda *p: None,
            on_text: Callable[[str], None] | None = None, ask: Callable[[str], str] | None = None, **_) -> str:
        """on_progress(label, fraction=None); ask(question) -> the user's spoken answer."""
        raise NotImplementedError


class OpenAIAgent(Agent):
    """Any OpenAI-compatible chat endpoint. Hermes Agent: url = http://host:8642/v1"""

    def __init__(self, cfg):
        super().__init__(cfg)
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        if cfg.session:
            headers["X-Hermes-Session-Key"] = cfg.session   # Hermes: scopes its long-term memory
        self.client = httpx.Client(base_url=cfg.url.rstrip("/"), headers=headers,
                                   timeout=httpx.Timeout(cfg.timeout_s, connect=5))
        self.history: list[dict] = []
        # Hermes keeps the conversation itself (X-Hermes-Session-Id), so jobs and the direct line share
        # context ("move the second one to 4"); a new one starts after new_conversation_after_min idle
        is_hermes = cfg.type.lower() == "hermes" or (cfg.model or "hermes-agent") == "hermes-agent"
        self.idle_s = cfg.new_conversation_after_min * 60 if is_hermes else 0
        # per-request speed settings Hermes documents (dropped if this Hermes rejects them)
        opts = {"reasoning_effort": cfg.reasoning_effort, "service_tier": cfg.service_tier}
        self.model_options = {k: v for k, v in opts.items() if v} if is_hermes else {}
        self.conversation = ""
        self.last_used = 0.0
        self.in_use = 0
        self.lock = threading.Lock()

    def new_conversation(self) -> None:
        with self.lock:
            self.conversation = ""

    def _begin(self) -> str:
        """The conversation this request continues, or "" to run on its own (another job is using it)."""
        if not self.idle_s:
            return ""
        with self.lock:
            now = time.monotonic()
            if not self.conversation or now - self.last_used > self.idle_s:
                self.conversation = f"{self.cfg.session or 'tesseract'}-{time.strftime('%Y%m%d-%H%M')}-{uuid.uuid4().hex[:4]}"
                log.info("agents: %s: new conversation %s", self.name, self.conversation)
            self.last_used = now
            if self.in_use:
                return ""
            self.in_use += 1
            return self.conversation

    def _end(self) -> None:
        with self.lock:
            self.in_use -= 1
            self.last_used = time.monotonic()

    def run(self, task, cancel, on_progress=lambda *p: None, on_text=None, keep_history=False, **_):
        conversation = self._begin()
        try:
            return self._run(task, cancel, on_progress, on_text, keep_history, conversation)
        finally:
            if conversation:
                self._end()

    def _run(self, task, cancel, on_progress, on_text, keep_history, conversation):
        messages = [{"role": "system", "content": VOICE_NOTE}]
        if keep_history and not conversation:       # with a conversation, Hermes already has the history
            messages += self.history[-12:]
        messages.append({"role": "user", "content": task})
        body = {"model": self.cfg.model or "hermes-agent", "messages": messages, "stream": True}
        if self.model_options:
            body["model_options"] = self.model_options
        headers = {"X-Hermes-Session-Id": conversation} if conversation else {}
        text, event = [], ""
        t0, first, steps = time.monotonic(), 0.0, 0
        with self.client.stream("POST", "/chat/completions", json=body, headers=headers) as r:
            if r.status_code in (400, 422) and self.model_options:
                log.warning("agents: %s rejected model_options %s (%r); sending without them", self.name,
                            self.model_options, r.read()[:200])
                self.model_options = {}
                return self._run(task, cancel, on_progress, on_text, keep_history, conversation)
            if r.status_code != 200:
                raise RuntimeError(f"{self.name} returned HTTP {r.status_code}: {r.read()[:200]!r}")
            for line in r.iter_lines():
                if line.startswith(("event:", "data:")) and not first:
                    first = time.monotonic() - t0
                if cancel.is_set():
                    return "".join(text)
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    if not line:
                        event = ""
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if event.endswith("tool.progress") or obj.get("object", "").endswith("tool.progress"):
                    tool = obj.get("tool") or obj.get("name") or obj.get("label") or obj.get("message") or ""
                    on_progress(step_words(tool))
                    steps += 1
                    continue
                for ch in obj.get("choices", []):
                    delta = (ch.get("delta") or {}).get("content")
                    if delta:
                        text.append(delta)
                        if on_text:
                            on_text(delta)
        reply = "".join(text).strip()
        log.info("agents: %s replied in %.1f s (first sign of life after %.1f s, %d tool step(s))",
                 self.name, time.monotonic() - t0, first, steps)
        if keep_history:
            self.history += [{"role": "user", "content": task}, {"role": "assistant", "content": reply}]
        return reply


class HomeAssistantAgent(Agent):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.client = httpx.Client(base_url=cfg.url.rstrip("/"), timeout=cfg.timeout_s,
                                   headers={"Authorization": f"Bearer {cfg.api_key}"})
        self.conversation_id = None

    def run(self, task, cancel, on_progress=lambda p: None, on_text=None, **_):
        body = {"text": task, "language": "en"}
        if self.conversation_id:
            body["conversation_id"] = self.conversation_id
        r = self.client.post("/api/conversation/process", json=body)
        r.raise_for_status()
        j = r.json()
        self.conversation_id = j.get("conversation_id")
        return j.get("response", {}).get("speech", {}).get("plain", {}).get("speech", "Done.")


class WebhookAgent(Agent):
    def __init__(self, cfg):
        super().__init__(cfg)
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        self.client = httpx.Client(timeout=cfg.timeout_s, headers=headers)

    def run(self, task, cancel, on_progress=lambda p: None, on_text=None, **_):
        r = self.client.post(self.cfg.url, json={"task": task, "source": "tesseract"})
        r.raise_for_status()
        try:
            j = r.json()
            return str(j.get("text") or j.get("response") or j.get("output") or j)
        except ValueError:
            return r.text


class CommandAgent(Agent):
    def run(self, task, cancel, on_progress=lambda p: None, on_text=None, **_):
        args = [a.replace("{task_quoted}", shlex.quote(task)).replace("{task}", task) for a in self.cfg.command]
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + self.cfg.timeout_s
        while True:
            # communicate() keeps draining stdout; a full pipe would otherwise block the child forever
            try:
                out, _ = p.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancel.is_set() or time.monotonic() > deadline:
                    p.terminate()
                    return "Stopped." if cancel.is_set() else "Timed out."
        out = (out or "").strip()
        return out[-3000:] or f"Finished with exit code {p.returncode}."


TYPES = {"openai": OpenAIAgent, "hermes": OpenAIAgent, "homeassistant": HomeAssistantAgent,
         "webhook": WebhookAgent, "command": CommandAgent}


# The LED progress bar. Agents rarely know how far along they are, and a bar that races to "nearly full"
# and then sits there for a minute is worse than none. So it grows with time, each new step counts
# as STEP_S more of it, and it never passes PROGRESS_CAP until the job is actually done.
PROGRESS_CAP = 0.8
PROGRESS_TAU_S = 60.0      # "time" to get ~63% of the way to the cap (half full after ~30 s of work)
STEP_S = 8.0


@dataclass
class Task:
    agent: str
    request: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:6])
    status: str = "running"          # running | waiting (for the user's answer) | done | failed | cancelled
    result: str = ""
    progress: str = ""               # what it's doing now, e.g. "Checking your calendar"
    steps: int = 0                   # distinct progress steps so far (Hermes reports a tool's start and end)
    hint: float = 0.0                # a fraction the agent gave itself, if any
    question: str = ""               # while waiting: what it asked
    announce: bool = True            # speak the result when it finishes
    builtin: bool = False            # the built-in agent writes results ready to speak
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    done: threading.Event = field(default_factory=threading.Event, repr=False)
    answers: queue.Queue = field(default_factory=queue.Queue, repr=False)

    @property
    def fraction(self) -> float:
        """0..1 for the LED progress bar: 1 once finished, otherwise never more than PROGRESS_CAP."""
        if self.status in ("done", "failed", "cancelled"):
            return 1.0
        effective = time.time() - self.started + STEP_S * self.steps
        grown = PROGRESS_CAP * (1 - math.exp(-effective / PROGRESS_TAU_S))
        return max(0.05, min(PROGRESS_CAP, max(grown, self.hint)))


class AgentManager:
    def __init__(self, cfg, on_finished: Callable[[Task], None], on_change: Callable[[], None] = lambda: None,
                 on_question: Callable[[Task], None] = lambda t: None, builtin: Agent | None = None):
        """builtin: the agent to use when config.toml lists none."""
        self.agents: dict[str, Agent] = {}
        for ac in cfg.agents:
            cls = TYPES.get(ac.type.lower())
            if not cls:
                log.error("agents: unknown type %r for %s", ac.type, ac.name)
                continue
            if ac.api_key_env and not ac.api_key:
                log.warning("agents: %s is missing %s in .env", ac.name, ac.api_key_env)
            self.agents[ac.name.lower()] = cls(ac)
        if builtin is not None and (not self.agents or cfg.thinking.keep_builtin):
            self.agents.setdefault(builtin.name.lower(), builtin)   # after the external ones: default() stays theirs
        self.input_timeout_s = cfg.thinking.input_timeout_s
        self.tasks: list[Task] = []
        self.on_finished = on_finished
        self.on_change = on_change
        self.on_question = on_question

    def get(self, name: str) -> Agent | None:
        n = name.strip().lower()
        if not n:
            return None   # "" is a substring of every name below
        if n in self.agents:
            return self.agents[n]
        for k, a in self.agents.items():  # tolerate "hermes agent", "the hermes"
            if k in n or n in k:
                return a
        return None

    def default(self) -> Agent | None:
        """Where a background job goes when nobody was named: the only agent, else the first."""
        return next(iter(self.agents.values()), None)

    def names(self) -> list[str]:
        return [a.name for a in self.agents.values()]

    def external(self) -> list[Agent]:
        return [a for a in self.agents.values() if not a.builtin]

    def running(self) -> list[Task]:
        return [t for t in self.tasks if t.status in ("running", "waiting")]

    def waiting(self) -> list[Task]:
        return [t for t in self.tasks if t.status == "waiting"]

    def start(self, agent: Agent, request: str, announce: bool = True) -> Task:
        task = Task(agent.name, request, announce=announce, builtin=agent.builtin)
        self.tasks = [t for t in self.tasks if t.status in ("running", "waiting")
                      or time.time() - t.finished < 3600][-20:]
        self.tasks.append(task)

        def progress(label, fraction=None):
            if str(label) != task.progress:      # a tool's "started" and "finished" are one step
                task.steps += 1
            task.progress = str(label)
            if fraction is not None:
                task.hint = fraction
            self.on_change()

        def ask(question: str) -> str:
            task.question, task.status = question, "waiting"
            self.on_change()
            self.on_question(task)
            deadline = time.monotonic() + self.input_timeout_s
            answer = None
            while answer is None and not task.cancel.is_set() and time.monotonic() < deadline:
                try:
                    answer = task.answers.get(timeout=0.5)
                except queue.Empty:
                    pass
            task.question, task.status = "", "running"
            self.on_change()
            return answer if answer is not None else "(The user didn't answer. Finish without it if you can.)"

        def go():
            try:
                task.result = agent.run(request, task.cancel, progress, ask=ask) or "Done."
                task.status = "cancelled" if task.cancel.is_set() else "done"
            except Exception as e:
                log.exception("agents: %s failed", agent.name)
                task.result = str(e)[:200] or type(e).__name__
                task.status = "failed"
            task.finished = time.time()
            task.done.set()
            self.on_change()
            if task.status != "cancelled":
                self.on_finished(task)

        threading.Thread(target=go, name=f"agent-{agent.name}", daemon=True).start()
        self.on_change()
        return task

    def answer(self, text: str, task: Task | None = None) -> bool:
        """Hand the user's spoken answer to a task that asked a question."""
        task = task or next(iter(self.waiting()), None)
        if task is None or task.status != "waiting":
            return False
        task.answers.put(text)
        return True

    def new_conversations(self) -> None:
        """Forget the context: external agents start a fresh conversation next time."""
        for a in self.agents.values():
            if hasattr(a, "new_conversation"):
                a.new_conversation()

    def cancel(self, name: str = "") -> list[Task]:
        hit = [t for t in self.running() if not name or name.lower() in t.agent.lower()]
        for t in hit:
            t.cancel.set()
        return hit

    def summary(self) -> str:
        if not self.tasks:
            return "No agent tasks yet."
        lines = []
        for t in self.tasks[-6:]:
            age = int((time.time() - t.started) / 60)
            line = f"{t.agent} [{t.status}] '{t.request[:80]}' started {age} min ago"
            if t.status == "running" and t.progress:
                line += f", currently: {t.progress}"
            if t.status == "waiting":
                line += f", waiting for the user to answer: {t.question}"
            if t.status in ("done", "failed"):
                line += f". Result: {t.result[:400]}"
            lines.append(line)
        return "\n".join(lines)

    def spoken_status(self) -> str:
        live = self.running()
        if not live:
            return "Nothing's running right now."
        t = live[-1]
        if t.status == "waiting":
            return f"I'm waiting on you: {t.question}"
        doing = t.progress[0].lower() + t.progress[1:] if t.progress else "working on it"
        more = f", and {len(live) - 1} more" if len(live) > 1 else ""
        return f"Still going. Right now I'm {doing}{more}."
