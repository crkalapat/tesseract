"""Who handles what: the fast model's tools, where background jobs go, and the
'speak the tool result directly' shortcut."""

import threading
import time

from tesseract import config
from tesseract.agents import AgentManager
from tesseract.alarms import Alarms
from tesseract.assistant import HANDOFF, Assistant, handoff_phrase
from tesseract.config import AgentConfig
from tesseract.thinker import ThinkingAgent, page_text


class Hooks:
    def set_volume(self, v): pass
    def volume(self): return 5
    def enter_agent_mode(self, n): self.mode = n
    def set_working(self, label): pass


class Speech:
    def __init__(self):
        self.said = []

    def say(self, s, cache=False):
        self.said.append(s)


def setup(tmp_path, agents=(), builtin=True, keep_builtin=False):
    cfg = config.load()
    cfg.agents = list(agents)
    cfg.thinking.keep_builtin = keep_builtin
    alarms = Alarms(tmp_path / "a.json", on_fire=print)
    b = ThinkingAgent(cfg, None, None, alarms, None) if builtin else None
    mgr = AgentManager(cfg, on_finished=lambda t: None, builtin=b)
    return cfg, alarms, mgr, Assistant(cfg, None, alarms, mgr, Hooks())


def tool_names(asst):
    return {t["function"]["name"]: t["function"] for t in asst.tools()}


def test_no_agents_uses_the_builtin(tmp_path):
    cfg, _, mgr, asst = setup(tmp_path)
    assert mgr.default().builtin and mgr.default().name == cfg.assistant.name
    tools = tool_names(asst)
    assert "agent" not in tools["start_task"]["parameters"]["properties"]   # nothing to choose between
    assert "talk_to_agent" not in tools and "answer_task" not in tools
    assert "to-do list, calendar or notes" in asst.system_prompt()


def test_external_agent_sits_alongside_the_builtin(tmp_path):
    hermes = AgentConfig(name="Hermes", type="openai", url="http://127.0.0.1:9/v1", description="research")
    cfg, _, mgr, asst = setup(tmp_path, [hermes], keep_builtin=True)
    assert mgr.names() == ["Hermes", cfg.assistant.name]
    assert mgr.default().name == "Hermes" and mgr.get(cfg.assistant.name).builtin
    start = tool_names(asst)["start_task"]["parameters"]
    assert start["properties"]["agent"]["enum"] == ["Hermes", cfg.assistant.name]
    assert start["required"] == ["request", "agent"]          # the fast model has to pick by description
    assert "Hermes: research" in asst.system_prompt() and "calendar (with reminders)" in asst.system_prompt()
    assert tool_names(asst)["talk_to_agent"]["parameters"]["properties"]["agent"]["enum"] == ["Hermes"]


def test_one_external_agent_gets_every_job(tmp_path):
    hermes = AgentConfig(name="Hermes", type="openai", url="http://127.0.0.1:9/v1", description="my agent")
    _, _, mgr, asst = setup(tmp_path, [hermes])            # the built-in agent is offered too, and dropped
    assert mgr.names() == ["Hermes"] and mgr.default().name == "Hermes"
    tools = tool_names(asst)
    assert "agent" not in tools["start_task"]["parameters"]["properties"]   # no need to say "ask Hermes"
    assert tools["talk_to_agent"]["parameters"]["properties"]["agent"]["enum"] == ["Hermes"]


def test_several_agents_are_named(tmp_path):
    a = AgentConfig(name="Hermes", type="openai", url="http://127.0.0.1:9/v1", description="research")
    b = AgentConfig(name="Home", type="homeassistant", url="http://127.0.0.1:9", description="lights")
    _, _, _, asst = setup(tmp_path, [a, b])
    assert tool_names(asst)["start_task"]["parameters"]["properties"]["agent"]["enum"] == ["Hermes", "Home"]
    assert "Hermes: research; Home: lights" in asst.system_prompt()


def test_spoken_tools_skip_the_second_round(tmp_path):
    _, alarms, _, asst = setup(tmp_path)
    rounds = []

    class Cloud:
        def chat_stream(self, messages, tools=None, cancel=None, **kw):
            rounds.append(messages[-1]["role"])
            yield "tool_calls", [{"id": "c1", "type": "function", "function": {
                "name": "set_timer", "arguments": '{"seconds": 300, "label": "tea"}'}}]
            yield "done", "tool_calls"

    asst.cloud = Cloud()
    speech = Speech()
    reply = asst.respond("set a tea timer for five minutes and um yeah", speech, threading.Event())
    assert rounds == ["user"]                          # one LLM call, not two
    assert speech.said == ["Tea timer set for 5 minutes."] == [reply]
    assert [t.label for t in alarms.timers()] == ["tea"]


def test_start_task_says_on_it_and_runs(tmp_path):
    _, _, mgr, asst = setup(tmp_path)
    ran = threading.Event()
    agent = mgr.default()
    agent.run = lambda task, cancel, on_progress=None, ask=None, **_: (ran.set(), "Added.")[1]

    class Cloud:
        def chat_stream(self, messages, tools=None, cancel=None, **kw):
            yield "tool_calls", [{"id": "c1", "type": "function", "function": {
                "name": "start_task", "arguments": '{"request": "add milk to my to-do list"}'}}]
            yield "done", "tool_calls"

    asst.cloud = Cloud()
    speech = Speech()
    asst.respond("add milk to my to-do list", speech, threading.Event())
    assert len(speech.said) == 1 and speech.said[0] in HANDOFF
    assert ran.wait(2) and mgr.tasks[0].request == "add milk to my to-do list"


def test_progress_and_questions(tmp_path):
    cfg, _, mgr, _ = setup(tmp_path)
    asked, fractions = [], []
    mgr.on_question = lambda t: asked.append(t.question)
    mgr.on_change = lambda: fractions.append(mgr.tasks[-1].fraction) if mgr.tasks else None
    agent = mgr.default()

    def run(task, cancel, on_progress=None, ask=None, **_):
        for label in ("Searching the web", "Reading a web page", "Adding it to your calendar"):
            on_progress(label)
        return f"Booked for {ask('What time?')}."

    agent.run = run
    task = mgr.start(agent, "book it")
    assert mgr_wait(lambda: mgr.waiting())
    assert asked == ["What time?"] and task.status == "waiting"
    assert mgr.answer("six") and task.done.wait(2)
    assert task.result == "Booked for six." and task.status == "done"
    steps = [f for f in fractions if f < 1]
    assert steps == sorted(steps) and steps[-1] < 0.9 and fractions[-1] == 1.0   # creeps up, never "done" early


def mgr_wait(cond, timeout=2):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_page_text():
    html = "<html><head><title>x</title><script>var a=1</script></head><body><h1>Club &amp; co</h1><p>Fri 6pm</p></body>"
    assert page_text(html) == "Club & co Fri 6pm"


def test_handoff_phrases_vary_and_stay_calm():
    said = [handoff_phrase() for _ in range(50)]
    assert all(a != b for a, b in zip(said, said[1:]))       # never the same twice in a row
    assert len(set(said)) >= 6 and not any("!" in p for p in HANDOFF)


def test_personal_requests_go_straight_to_hermes(tmp_path):
    from tesseract.app import normalize
    hermes = AgentConfig(name="Hermes", type="openai", url="http://127.0.0.1:9/v1", description="my agent")
    _, _, _, asst = setup(tmp_path, [hermes])
    straight = ["Do I have any new emails?", "What's on my calendar Friday?", "How's my week looking?",
                "Add 12 dollars for lunch to my budget", "Find the physics notes in my Google Drive",
                "What's in my spreadsheet for rent?", "Read my inbox", "When's my next meeting?",
                "Add milk to my to-do list", "Franklin, check my e-mail"]
    for s in straight:
        assert asst.route_personal(normalize(s, "franklin")), s
    for s in ["Set a timer for my meeting", "What's the weather tomorrow?", "Who won the World Series?",
              "Play my study playlist", "Let me talk to Hermes", "Set an alarm for my calendar thing at 7"]:
        assert asst.route_personal(normalize(s, "franklin")) is None, s
    _, _, _, builtin_only = setup(tmp_path)
    assert builtin_only.route_personal("check my email") is None        # no Hermes: the fast model decides


def test_agent_request_carries_the_conversation_just_before(tmp_path):
    hermes = AgentConfig(name="Hermes", type="openai", url="http://127.0.0.1:9/v1", description="my agent")
    cfg, _, _, asst = setup(tmp_path, [hermes])
    assert asst.agent_request("add it to my calendar") == "add it to my calendar"
    asst.remember("what's the weather Saturday?", "Sunny, 75.")
    req = asst.agent_request("add a picnic on that day to my calendar")
    assert req.startswith("add a picnic") and "weather Saturday" in req and "Sunny, 75." in req


def test_progress_bar_never_looks_done_early():
    from tesseract.agents import PROGRESS_CAP, Task
    t = Task("Hermes", "plan my week")
    assert 0.05 <= t.fraction < 0.1                       # just started
    t.started -= 30
    assert 0.3 < t.fraction < 0.5                         # about half the way to the cap after 30 s
    t.steps = 40                                          # a chatty agent: lots of tool events
    t.started -= 600                                      # and a long job
    assert t.fraction <= PROGRESS_CAP                     # still clearly not done
    t.hint = 0.99                                         # even if the agent claims it's nearly there
    assert t.fraction == PROGRESS_CAP
    t.status = "done"
    assert t.fraction == 1.0


def test_saying_on_it_without_calling_the_tool_still_starts_the_job(tmp_path):
    """gpt-oss once answered "On it. On it." as plain text and called nothing: the request was dropped."""
    hermes = AgentConfig(name="Hermes", type="openai", url="http://127.0.0.1:9/v1", description="my agent")
    _, _, mgr, asst = setup(tmp_path, [hermes])
    started = []
    mgr.start = lambda agent, request, announce=True: started.append((agent.name, request))

    class Cloud:
        def chat_stream(self, messages, tools=None, cancel=None, **kw):
            yield "text", "On it. On it."
            yield "done", "stop"

    asst.cloud = Cloud()
    reply = asst.respond("How do I look in this jacket, and also what did the vet say", Speech(), threading.Event())
    assert reply == "On it. On it." and started and started[0][0] == "Hermes"
    started.clear()

    class Answer:
        def chat_stream(self, messages, tools=None, cancel=None, **kw):
            yield "text", "Paris is the capital of France."
            yield "done", "stop"

    asst.cloud = Answer()
    asst.respond("What's the capital of France?", Speech(), threading.Event())
    assert not started                                  # a real answer starts nothing


def test_money_questions_go_straight_to_hermes(tmp_path):
    from tesseract.app import normalize
    hermes = AgentConfig(name="Hermes", type="openai", url="http://127.0.0.1:9/v1", description="my agent")
    _, _, _, asst = setup(tmp_path, [hermes])
    for s in ["How much money did I spend at the movie theater the other day?", "Did I pay the phone bill yet?",
              "What's my bank balance?", "Show me my recent purchases"]:
        assert asst.route_personal(normalize(s, "franklin")), s
