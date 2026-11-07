"""OpenRouter client: speech-to-text, streaming chat (with tools), streaming TTS.

Speed notes:
  * One long-lived HTTP/2 connection. The TLS handshake to openrouter.ai costs
    a few hundred ms on a Pi Zero, so `warm()` opens it the moment the wake
    word fires, while you're still talking.
  * Everything streams. The LLM's first sentence goes to TTS before the rest
    of the reply exists (see speech.py).
  * provider.sort = "latency" asks OpenRouter for the fastest provider.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from typing import Iterator

import httpx

from .audio import to_wav_bytes

log = logging.getLogger(__name__)

BASE = "https://openrouter.ai/api/v1"


class _NotStarted(Exception):
    """The local model didn't answer at all, so the cloud can take the request instead."""


class CloudError(Exception):
    """An error with a message short enough to show on the display / speak."""


def _friendly(status: int, body: str) -> CloudError:
    msg = {401: "API key rejected", 402: "Out of OpenRouter credits", 429: "Rate limited",
           408: "Request timed out", 502: "Model provider is down", 503: "Service unavailable"}
    try:
        detail = json.loads(body).get("error", {}).get("message", "")
    except Exception:
        detail = body[:200]
    log.error("cloud: HTTP %s: %s", status, detail)
    return CloudError(msg.get(status, f"Cloud error {status}"))


def _provider(order, sort) -> dict | None:
    """OpenRouter provider routing: try `order` first (falling back to any other
    host), else let OpenRouter pick by `sort` ("latency", "price", "throughput")."""
    if order:
        return {"order": list(order), "allow_fallbacks": True}
    if sort:
        return {"sort": sort}
    return None


_TAGS = re.compile(r"\*[^*]*\*|\[[^\]]*\]|\([^)]*\)")


# What Whisper says when it's given silence or room noise: it learned these from video subtitles
_GHOST = {"thank you", "thanks", "thank you so much", "thank you very much", "thank you for watching",
          "thanks for watching", "bye", "bye bye", "you", "please subscribe", "subscribe", "thank you bye"}
_NEVER_SAID = {"thank you for watching", "thanks for watching", "please subscribe", "subscribe"}


def drop_ghosts(text: str) -> str:
    """Remove the run of ghost sentences Whisper tacks onto the end: "What's the weather? Thank you.
    Thank you. Thank you." -> "What's the weather?". A single "Thank you." stays (you might mean
    it), unless it's one nobody says to a speaker ("Thanks for watching")."""
    parts = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    key = lambda s: re.sub(r"[^a-z ]", "", s.lower()).strip()
    tail = 0
    while tail < len(parts) and key(parts[len(parts) - 1 - tail]) in _GHOST:
        tail += 1
    ghosts = [key(s) for s in parts[len(parts) - tail:]]
    if tail and (tail >= 2 or ghosts[0] in _NEVER_SAID):
        parts = parts[:len(parts) - tail]
    out = " ".join(parts)
    # the same thing unpunctuated: "turn it up thank you thank you thank you"
    return re.sub(r"(?:[\s,.!]*\b(?:thank you|thanks)\b[.!]?){2,}\s*$", "", out, flags=re.I).strip()


def clean_transcript(text: str) -> str:
    """Drop Whisper's sound tags ("*phone rings*" for the wake chime, "[Music]", "(laughs)"), the
    "Thank you. Thank you." it invents for trailing noise, and stray leading punctuation. Left in,
    "*phone rings*" once made the model cancel a timer."""
    text = _TAGS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip().lstrip(".,;:!?- ").strip()
    return drop_ghosts(text)


def _http2() -> bool:
    try:
        import h2  # noqa: F401
        return True
    except ImportError:
        return False


class OpenRouter:
    def __init__(self, cfg):
        self.cfg = cfg
        headers = {
            "Authorization": f"Bearer {cfg.openrouter_key}",
            "HTTP-Referer": "https://github.com/crkalapat/tesseract",
            "X-Title": "Tesseract",
        }
        self.client = httpx.Client(
            base_url=BASE,
            http2=_http2(),
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300),
            headers=headers,
        )
        self._local: httpx.Client | None = None        # [local_llm], made when first used
        self._local_down_until = 0.0
        # Speech-to-text gets its own HTTP/1.1 connections: speculative transcriptions overlap,
        # and two of them sharing the HTTP/2 connection stalled one for 10+ seconds on the Pi.
        # A short timeout means a stuck request fails fast instead of holding up the turn.
        self.stt_client = httpx.Client(
            base_url=BASE,
            http2=False,
            timeout=httpx.Timeout(8.0, connect=4.0),
            limits=httpx.Limits(max_keepalive_connections=3, keepalive_expiry=300),
            headers=headers,
        )

    def warm(self) -> None:
        """Open the TLS connection in the background so the real request doesn't pay for it."""
        def go(client):
            try:
                client.get("/key", timeout=5)
            except Exception as e:
                log.debug("cloud: warm-up failed: %s", e)
        for client in (self.client, self.stt_client):
            threading.Thread(target=go, args=(client,), name="cloud-warm", daemon=True).start()

    # ── Speech-to-text ──────────────────────────────────────────────────────
    def transcribe(self, pcm16) -> str:
        body = {
            "model": self.cfg.stt.model,
            "input_audio": {"data": base64.b64encode(to_wav_bytes(pcm16)).decode(), "format": "wav"},
        }
        if self.cfg.stt.language:
            body["language"] = self.cfg.stt.language
        if provider := _provider(self.cfg.stt.provider_order, ""):
            body["provider"] = provider
        t0 = time.monotonic()
        try:
            r = self.stt_client.post("/audio/transcriptions", json=body)
        except httpx.TimeoutException as e:
            log.warning("cloud: speech-to-text timed out after %.1fs", time.monotonic() - t0)
            raise CloudError("Speech-to-text timed out") from e
        except httpx.HTTPError as e:
            raise CloudError("No connection") from e
        if r.status_code != 200:
            raise _friendly(r.status_code, r.text)
        text = clean_transcript(r.json().get("text") or "")
        log.info("stt: %.2fs for %.1fs of audio", time.monotonic() - t0, len(pcm16) / 16000)
        return text

    # ── Chat ────────────────────────────────────────────────────────────────
    def _chat_body(self, messages, tools=None, llm=None, model=None, stream=True, **extra) -> dict:
        """llm: cfg.llm (the fast model, the default) or cfg.thinking."""
        L = llm or self.cfg.llm
        body = {"model": model or L.model, "messages": messages, "stream": stream, "max_tokens": L.max_tokens}
        if L.temperature is not None:
            body["temperature"] = L.temperature
        if tools:
            body["tools"] = tools
        if provider := _provider(L.provider_order, L.provider_sort):
            body["provider"] = provider
        if L.reasoning:
            effort = "none" if L.reasoning == "off" else L.reasoning
            body["reasoning"] = {"effort": effort, "exclude": True}
        body.update(extra)
        return body

    # ── a model on your own computer ([local_llm]) ──────────────────────────
    def _use_local(self, llm, model) -> bool:
        """Local mode covers the fast model (and with use_for = "all", the background agent), never
        web search, which needs OpenRouter's search plugin. A computer that didn't answer is
        skipped for a minute."""
        L = self.cfg.local_llm
        if not (L.enabled and L.url and L.model) or model is not None or time.monotonic() < self._local_down_until:
            return False
        return llm is None or llm is self.cfg.llm or (L.use_for == "all" and llm is self.cfg.thinking)

    def _local_client(self) -> httpx.Client:
        L = self.cfg.local_llm
        if self._local is None or str(self._local.base_url).rstrip("/") != L.url.rstrip("/"):
            headers = {"Authorization": f"Bearer {L.api_key}"} if L.api_key else {}
            self._local = httpx.Client(base_url=L.url.rstrip("/"), headers=headers,
                                       timeout=httpx.Timeout(60.0, connect=3.0))
        return self._local

    def _local_body(self, body: dict) -> dict:
        b = {k: v for k, v in body.items() if k not in ("provider", "reasoning", "plugins")}
        b["model"] = self.cfg.local_llm.model
        b["max_tokens"] = max(b.get("max_tokens") or 0, self.cfg.local_llm.max_tokens)
        return b

    def _local_failed(self, e: Exception) -> None:
        if not self.cfg.local_llm.fallback:
            raise CloudError("Can't reach the local model") from e
        log.warning("llm: local model at %s didn't answer (%s); using the cloud for a minute",
                    self.cfg.local_llm.url, e)
        self._local_down_until = time.monotonic() + 60

    def chat_stream(self, messages, tools=None, cancel: threading.Event | None = None,
                    llm=None, model: str | None = None, **extra) -> Iterator[tuple[str, object]]:
        """Yields ("text", str) deltas, then ("tool_calls", [...]) if the model
        called tools, then ("done", finish_reason)."""
        body = self._chat_body(messages, tools, llm, model, **extra)
        if self._use_local(llm, model):
            try:
                yield from self._stream(self._local_client(), self._local_body(body), cancel, local=True)
                return
            except _NotStarted as e:
                self._local_failed(e.__cause__ or e)
        yield from self._stream(self.client, body, cancel)

    def _stream(self, client, body, cancel, local=False):
        calls: dict[int, dict] = {}
        finish = None
        started = False
        try:
            with client.stream("POST", "/chat/completions", json=body,
                               timeout=httpx.Timeout(60.0, connect=3.0 if local else 5.0)) as r:
                if r.status_code != 200:
                    err = _friendly(r.status_code, r.read().decode(errors="replace"))
                    if local:
                        raise _NotStarted() from err
                    raise err
                for line in r.iter_lines():
                    if cancel is not None and cancel.is_set():
                        return
                    if not line.startswith("data:"):
                        continue  # SSE comments like ": OPENROUTER PROCESSING"
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if "error" in chunk:
                        err = chunk["error"]
                        raise CloudError((err.get("message", "Model error") if isinstance(err, dict) else str(err))[:80])
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            started = True
                            yield "text", delta["content"]
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", 0),
                                                    {"id": "", "type": "function",
                                                     "function": {"name": "", "arguments": ""}})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            slot["function"]["name"] += fn.get("name") or ""
                            slot["function"]["arguments"] += fn.get("arguments") or ""
                        finish = choice.get("finish_reason") or finish
        except httpx.HTTPError as e:
            if local and not started:
                raise _NotStarted() from e
            raise CloudError("No connection") from e
        if calls:
            yield "tool_calls", [calls[i] for i in sorted(calls)]
        yield "done", finish

    def complete(self, messages, tools=None, llm=None, model: str | None = None, **extra) -> dict:
        """One non-streamed reply: the assistant message, including any tool_calls."""
        body = self._chat_body(messages, tools, llm, model, stream=False, **extra)
        if self._use_local(llm, model):
            try:
                r = self._local_client().post("/chat/completions", json=self._local_body(body), timeout=180)
                if r.status_code == 200 and "choices" in r.json():
                    return r.json()["choices"][0]["message"]
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
            except (httpx.HTTPError, RuntimeError, ValueError) as e:
                self._local_failed(e)
        try:
            r = self.client.post("/chat/completions", json=body, timeout=120)
        except httpx.HTTPError as e:
            raise CloudError("No connection") from e
        if r.status_code != 200:
            raise _friendly(r.status_code, r.text)
        j = r.json()
        if "error" in j:
            raise CloudError(str(j["error"].get("message", "Model error"))[:80])
        return j["choices"][0]["message"]

    def chat(self, messages, llm=None, model: str | None = None, **extra) -> str:
        return (self.complete(messages, llm=llm, model=model, **extra).get("content") or "").strip()

    # ── Text-to-speech ──────────────────────────────────────────────────────
    def speech_stream(self, text: str, cancel: threading.Event | None = None) -> Iterator[bytes]:
        """Raw 16-bit little-endian mono PCM at cfg.tts.sample_rate, as it arrives."""
        T = self.cfg.tts
        body = {"model": T.model, "input": text, "voice": T.voice, "response_format": "pcm"}
        if T.speed != 1.0:
            body["speed"] = T.speed
        try:
            with self.client.stream("POST", "/audio/speech", json=body) as r:
                if r.status_code != 200:
                    raise _friendly(r.status_code, r.read().decode(errors="replace"))
                for chunk in r.iter_bytes():
                    if cancel is not None and cancel.is_set():
                        return
                    yield chunk
        except httpx.HTTPError as e:
            raise CloudError("No connection") from e
