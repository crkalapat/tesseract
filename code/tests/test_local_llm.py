"""Local mode: the fast model on your own computer, the cloud when it's away; Hermes added from the CLI."""

import httpx
import pytest

from tesseract import config, settings
from tesseract.cloud import OpenRouter


def _cfg(tmp_path):
    cfg = config.load()
    cfg.local = tmp_path / "local.toml"
    L = cfg.local_llm
    L.enabled, L.url, L.model = True, "http://mac.local:1234/v1", "qwen/qwen3.5-9b"
    return cfg


def _sse(*texts):
    import json
    body = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n" for t in texts)
    return body + "data: [DONE]\n\n"


def test_fast_model_goes_local_and_search_stays_in_the_cloud(tmp_path):
    cfg = _cfg(tmp_path)
    cloud = OpenRouter(cfg)
    seen = []

    def handler(req):
        import json
        seen.append((req.url.host, json.loads(req.content)))
        return httpx.Response(200, text=_sse("Hi", " there"), headers={"content-type": "text/event-stream"})
    cloud.client = httpx.Client(base_url="https://openrouter.ai/api/v1", transport=httpx.MockTransport(handler))
    cloud._local = httpx.Client(base_url=cfg.local_llm.url, transport=httpx.MockTransport(handler))
    text = "".join(v for k, v in cloud.chat_stream([{"role": "user", "content": "hi"}]) if k == "text")
    assert text == "Hi there" and seen[-1][0] == "mac.local"
    body = seen[-1][1]
    assert body["model"] == "qwen/qwen3.5-9b" and "provider" not in body and "reasoning" not in body
    list(cloud.chat_stream([{"role": "user", "content": "news"}], model=cfg.llm.search_model,
                           plugins=[{"id": "web"}]))
    assert seen[-1][0] == "openrouter.ai"                      # web search needs OpenRouter's plugin
    list(cloud.chat_stream([{"role": "user", "content": "job"}], llm=cfg.thinking))
    assert seen[-1][0] == "openrouter.ai"                      # use_for = "fast": the agent stays in the cloud


def test_computer_asleep_falls_back_to_the_cloud(tmp_path):
    cfg = _cfg(tmp_path)
    cloud = OpenRouter(cfg)
    hosts = []

    def down(req):
        hosts.append(req.url.host)
        raise httpx.ConnectError("no route to host")

    def up(req):
        hosts.append(req.url.host)
        return httpx.Response(200, text=_sse("From the cloud"), headers={"content-type": "text/event-stream"})
    cloud._local = httpx.Client(base_url=cfg.local_llm.url, transport=httpx.MockTransport(down))
    cloud.client = httpx.Client(base_url="https://openrouter.ai/api/v1", transport=httpx.MockTransport(up))
    text = "".join(v for k, v in cloud.chat_stream([{"role": "user", "content": "hi"}]) if k == "text")
    assert text == "From the cloud" and hosts == ["mac.local", "openrouter.ai"]
    list(cloud.chat_stream([{"role": "user", "content": "again"}]))
    assert hosts[-1] == "openrouter.ai" and hosts.count("mac.local") == 1     # not retried for a minute


def test_hermes_saved_on_the_device(tmp_path, monkeypatch):
    cfg = config.load()
    cfg.local = tmp_path / "local.toml"
    settings.set_local_agent(cfg, "Hermes", {"name": "Hermes", "type": "openai", "url": "http://a:8642/v1"})
    settings.set_local_agent(cfg, "Hermes", {"name": "Hermes", "type": "openai", "url": "http://b:8642/v1"})
    settings.set_local_value(cfg, "local_llm", "enabled", True)
    src = tmp_path / "config.toml"
    src.write_text(config.CODE_DIR.joinpath("config.toml").read_text())
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "local.toml").write_text(cfg.local.read_text())
    loaded = config.load(src)
    assert [a.url for a in loaded.agents if a.name == "Hermes"] == ["http://b:8642/v1"] and loaded.local_llm.enabled
    settings.set_local_agent(cfg, "Hermes", None)
    assert "Hermes" not in cfg.local.read_text()
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=x\nHERMES_API_KEY=old\n")
    monkeypatch.setenv("HERMES_API_KEY", "")
    settings.set_env_value(env, "HERMES_API_KEY", "new")
    assert env.read_text() == "OPENROUTER_API_KEY=x\nHERMES_API_KEY=new\n"


@pytest.mark.parametrize("given,port,want", [("192.168.1.50", 8642, "http://192.168.1.50:8642/v1"),
                                             ("http://mac.local:1234/v1", 1234, "http://mac.local:1234/v1"),
                                             ("mac.local:1235", 1234, "http://mac.local:1235/v1"),
                                             ("https://franklin.example.ts.net", 8642,
                                              "https://franklin.example.ts.net/v1")])
def test_addresses(given, port, want):
    from tesseract.__main__ import _v1_url
    assert _v1_url(given, port) == want


def test_reads_the_franklin_env_file(tmp_path):
    from tesseract.__main__ import _read_env_file
    f = tmp_path / "franklin.env"
    f.write_text('# Hermes on franklin\nFRANKLIN_URL=https://franklin.example.ts.net\nexport FRANKLIN_API_KEY="s3cr=t"\n')
    assert _read_env_file(f) == {"FRANKLIN_URL": "https://franklin.example.ts.net", "FRANKLIN_API_KEY": "s3cr=t"}
    assert _read_env_file(tmp_path / "missing.env") == {}
