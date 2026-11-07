"""`tesseract routing-test [MODEL ...]`: how well the fast model picks the right tool, and how fast.

Runs realistic requests through the real system prompt and tools (with Hermes connected, as on the
Pi), once per model, and scores the first thing each model does: answer itself, or which tool. The
point is the two mistakes that hurt: answering from memory when it should search (made-up facts), and
saying "I can't" instead of using a tool or handing off.
"""

from __future__ import annotations

import dataclasses
import statistics
import threading
import time

# (what you say, the acceptable first moves; "answer" = it replies itself)
CASES = [
    ("Who won the World Series this year?", {"web_search"}),
    ("What's the population of Japan?", {"web_search"}),
    ("Who is the CEO of OpenAI?", {"web_search"}),
    ("What's the price of Bitcoin right now?", {"web_search"}),
    ("When does Target close tonight?", {"web_search"}),
    ("What's the newest iPhone?", {"web_search"}),
    ("How many points did LeBron score last night?", {"web_search"}),
    ("What year did the James Webb telescope launch?", {"web_search", "answer"}),
    ("Who wrote Pride and Prejudice?", {"answer", "web_search"}),
    ("What's fifteen percent of eighty?", {"answer"}),
    ("How does photosynthesis work?", {"answer"}),
    ("Tell me a joke.", {"answer"}),
    ("How do I hard boil an egg?", {"answer"}),
    ("What's the weather this weekend?", {"get_weather"}),
    ("Is it going to rain tomorrow?", {"get_weather"}),
    ("Set the volume to three.", {"set_volume"}),
    ("Wake me up at seven tomorrow.", {"set_alarm"}),
    ("Can you stop listening for a bit?", {"mute"}),
    ("Mute the sound.", {"mute"}),
    ("Turn on quiet mode.", {"quiet_mode"}),
    ("Hold the results until I ask, don't read them out.", {"quiet_mode"}),
    ("Am I free Thursday afternoon?", {"start_task"}),
    ("Did my professor reply yet?", {"start_task"}),
    ("How much have I spent on food this month?", {"start_task"}),
    ("Add eggs to the shopping list.", {"start_task"}),
    ("Move my dentist appointment to Friday.", {"start_task"}),
    ("What did I say I wanted for my birthday?", {"start_task"}),
    ("Text Sam that I'm running late.", {"start_task"}),
    ("How much money did I spend at the movie theater the other day?", {"start_task"}),
    ("Did I pay the phone bill yet?", {"start_task"}),
    ("Find a good Thai place nearby and book a table for two at seven.", {"start_task"}),
    ("Let me talk to Hermes.", {"talk_to_agent"}),
]


class _Hooks:
    spotify = None
    def screen(self): return 10
    def volume(self): return 5
    def held_count(self): return 0


def first_move(cloud, asst, llm, text: str) -> tuple[str, float, str]:
    """(answer | tool name, seconds to the first token, what it said)"""
    messages = [{"role": "system", "content": asst.system_prompt()}, {"role": "user", "content": text}]
    t0, first, said = time.monotonic(), None, ""
    for kind, val in cloud.chat_stream(messages, tools=asst.tools(), cancel=threading.Event(), llm=llm):
        if first is None and kind in ("text", "tool_calls"):
            first = time.monotonic() - t0
        if kind == "text":
            said += val
        elif kind == "tool_calls":
            fn = val[0]["function"]
            return fn["name"], first or 0.0, fn.get("arguments", "")[:80]
    return "answer", first if first is not None else time.monotonic() - t0, said.strip()[:80]


def run(cfg, models: list[str]) -> int:
    from .agents import AgentManager
    from .alarms import Alarms
    from .assistant import Assistant
    from .cloud import OpenRouter
    from .config import AgentConfig
    import tempfile
    from pathlib import Path

    if not cfg.openrouter_key:
        print("No OPENROUTER_API_KEY in .env, so there's nothing to test against.")
        return 1
    cfg.local_llm.enabled = False
    if not any(a.type in ("openai", "hermes") for a in cfg.agents):
        cfg.agents = [AgentConfig(name="Hermes", type="openai", url="http://127.0.0.1:9/v1", model="hermes-agent",
                                  description="My agent: my Google Calendar, Docs, Sheets, email, memory, research.")]
    cloud = OpenRouter(cfg)
    alarms = Alarms(Path(tempfile.mkdtemp()) / "alarms.json", on_fire=lambda *a: None)
    asst = Assistant(cfg, cloud, alarms, AgentManager(cfg, on_finished=lambda t: None), _Hooks())
    models = models or [cfg.llm.model]
    summary = []
    for model in models:
        # the provider list in config.toml is for gpt-oss; other models go to wherever OpenRouter finds fastest
        llm = cfg.llm if model == cfg.llm.model else dataclasses.replace(cfg.llm, model=model, provider_order=[],
                                                                          provider_sort="latency")
        print(f"\n== {model}")
        right, times, wrong = 0, [], []
        for text, ok in CASES:
            try:
                move, t, said = first_move(cloud, asst, llm, text)
            except Exception as e:
                move, t, said = "error", 0.0, str(e)[:80]
            good = move in ok
            right += good
            if move != "error":
                times.append(t)
            print(f"  {'ok ' if good else 'BAD'} {t:4.2f}s  {text[:52]:52} -> {move} {'' if good else said!r}")
            if not good:
                wrong.append(text)
        med = statistics.median(times) if times else 0.0
        summary.append((model, right, med, max(times, default=0.0)))
    print("\nmodel                                     right   median first token   worst")
    for model, right, med, worst in summary:
        print(f"{model:40} {right:3}/{len(CASES)}   {med:5.2f} s               {worst:5.2f} s")
    return 0
