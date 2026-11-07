"""OpenAI-compatible agent client against a fake Hermes-style SSE server."""

import json
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tesseract.agents import AgentManager, CommandAgent, OpenAIAgent
from tesseract.config import AgentConfig

received = {}


class FakeHermes(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        received["body"] = body
        received["headers"] = dict(self.headers)
        if "model_options" in body and received.get("reject_options"):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "unknown field model_options"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(s):
            self.wfile.write(s.encode())
            self.wfile.flush()

        send(": keepalive\n\n")
        send('event: hermes.tool.progress\ndata: {"tool": "terminal", "label": "Running df -h"}\n\n')
        for tok in ["Disk is ", "42% full.", " All good."]:
            send("data: " + json.dumps({"choices": [{"delta": {"content": tok}}]}) + "\n\n")
        send("data: [DONE]\n\n")


class QuickServer(ThreadingHTTPServer):
    def server_bind(self):  # skip HTTPServer's slow reverse-DNS lookup
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


@pytest.fixture
def server():
    srv = QuickServer(("127.0.0.1", 0), FakeHermes)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_openai_agent_streams_and_reports_progress(server, monkeypatch):
    monkeypatch.setenv("HERMES_API_KEY", "secret")
    agent = OpenAIAgent(AgentConfig(name="Hermes", type="openai", url=server, api_key_env="HERMES_API_KEY",
                                    model="hermes-agent"))
    progress, deltas = [], []
    out = agent.run("how full is my disk?", threading.Event(), progress.append, deltas.append, keep_history=True)
    assert out == "Disk is 42% full. All good."
    assert progress == ["Running code"]          # the tool ("terminal") in words, for the screen
    assert "".join(deltas) == out
    assert received["headers"]["Authorization"] == "Bearer secret"
    assert received["headers"]["X-Hermes-Session-Key"] == "tesseract"
    assert received["body"]["messages"][-1] == {"role": "user", "content": "how full is my disk?"}
    first = received["headers"]["X-Hermes-Session-Id"]
    # the next turn continues the same Hermes conversation, which holds the history server-side
    agent.run("and now?", threading.Event(), keep_history=True)
    assert received["headers"]["X-Hermes-Session-Id"] == first
    assert [m["role"] for m in received["body"]["messages"]] == ["system", "user"]


def test_hermes_conversation_restarts_after_idle_or_reset(server):
    agent = OpenAIAgent(AgentConfig(name="Hermes", type="openai", url=server, model="hermes-agent"))
    sid = lambda: received["headers"]["X-Hermes-Session-Id"]
    agent.run("a", threading.Event())
    first = sid()
    assert first.startswith("tesseract-")
    agent.last_used -= 14 * 60
    agent.run("b", threading.Event())
    assert sid() == first                    # 14 min idle: same conversation
    agent.last_used -= 16 * 60
    agent.run("c", threading.Event())
    second = sid()
    assert second != first                   # 16 min idle: a new one
    agent.new_conversation()                 # holding the action button
    agent.run("d", threading.Event())
    assert sid() not in (first, second)


def test_overlapping_jobs_dont_share_a_conversation(server):
    agent = OpenAIAgent(AgentConfig(name="Hermes", type="openai", url=server, model="hermes-agent"))
    assert agent._begin()                    # a job is using the conversation...
    agent.run("another job", threading.Event())
    assert "X-Hermes-Session-Id" not in received["headers"]               # ...so this one runs on its own
    agent._end()
    agent.run("next", threading.Event())
    assert "X-Hermes-Session-Id" in received["headers"]


def test_other_openai_servers_keep_history_locally(server):
    agent = OpenAIAgent(AgentConfig(name="Box", type="openai", url=server, model="qwen"))
    agent.run("hi", threading.Event(), keep_history=True)
    agent.run("and now?", threading.Event(), keep_history=True)
    assert "X-Hermes-Session-Id" not in received["headers"]
    assert [m["role"] for m in received["body"]["messages"]] == ["system", "user", "assistant", "user"]


def test_manager_background_task(server):
    from tesseract import config
    cfg = config.load()
    cfg.agents = [AgentConfig(name="Hermes", type="openai", url=server, description="test")]
    done = threading.Event()
    finished = []
    mgr = AgentManager(cfg, on_finished=lambda t: (finished.append(t), done.set()))
    assert mgr.get("the hermes agent").name == "Hermes"
    task = mgr.start(mgr.get("hermes"), "check disk", announce=True)
    assert done.wait(5)
    assert task.status == "done" and "42%" in task.result
    assert "Hermes [done]" in mgr.summary()


def test_command_agent_large_output():
    # more output than a pipe buffer holds (64 KB): the agent must keep reading, not deadlock
    agent = CommandAgent(AgentConfig(name="Big", type="command", timeout_s=10,
                                     command=[sys.executable, "-c", "print('x' * 200_000); print('end')"]))
    assert agent.run("", threading.Event()).endswith("end")


def test_hermes_gets_speed_options_and_copes_without_them(server):
    agent = OpenAIAgent(AgentConfig(name="Hermes", type="openai", url=server, model="hermes-agent"))
    agent.run("hi", threading.Event())
    assert received["body"]["model_options"] == {"reasoning_effort": "low"}
    received["reject_options"] = True
    try:
        assert agent.run("hi", threading.Event()) == "Disk is 42% full. All good."   # retried without them
        assert "model_options" not in received["body"]
    finally:
        received.pop("reject_options")
    other = OpenAIAgent(AgentConfig(name="Box", type="openai", url=server, model="qwen"))
    other.run("hi", threading.Event())
    assert "model_options" not in received["body"]                    # only Hermes knows them


def test_tool_calls_in_words():
    from tesseract.agents import step_words
    assert step_words("mcp_google_get_events") == "Checking calendar"
    assert step_words("manage_event") == "Updating calendar"
    assert step_words("🔍 web_search: weather in austin") == "Searching the web"
    assert step_words("search_drive_files") == "Searching Drive"
    assert step_words("modify_sheet_values") == "Updating a sheet"
    assert step_words("browser_exec") == "Using the browser"
    assert step_words("mcp_weather_get_forecast_now") == "Get forecast now"     # unknown: the name, readable
