"""Turns a stream of LLM text into a stream of speech, as early as possible.

    LLM tokens ─► SentenceSplitter ─► TTS fetch (2 in flight) ─► in-order ─► Mixer
                                       sentence 1 streams straight to the speaker
                                       while sentence 2 is already downloading

The first audible word comes out after: STT + LLM time-to-first-sentence +
TTS time-to-first-byte, instead of after the whole reply is generated and
synthesized.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time

import numpy as np

from . import dsp
from .audio import HW_RATE
from .playback import StreamSource

log = logging.getLogger(__name__)

_ABBREV = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "approx", "jr", "sr",
           "prof", "inc", "ltd", "mt", "ft", "u.s", "a.m", "p.m", "no",
           "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
           "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun"}
_END = re.compile(r"([.!?…]+[\"')\]]*)(\s+)|(\n+)")


class SentenceSplitter:
    """Incrementally cuts streamed text into speakable sentences."""

    def __init__(self, first_clause_chars: int = 60):
        self.buf = ""
        self.first = True
        self.first_clause_chars = first_clause_chars

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            s, self.buf = self.buf[:cut].strip(), self.buf[cut:]
            if s:
                out.append(s)
                self.first = False
        return out

    def flush(self) -> list[str]:
        s, self.buf = self.buf.strip(), ""
        return [s] if s else []

    def _find_cut(self) -> int | None:
        for m in _END.finditer(self.buf):
            if m.group(3):  # newline
                return m.end()
            end = m.end(1)
            word = re.search(r"([\w.]+)[.]$", self.buf[:end])
            if word and m.group(1) == "." and (
                    word.group(1).lower() in _ABBREV or len(word.group(1)) == 1):
                continue  # "Dr. Smith", "J. R. R."
            return m.end()
        # Long opening clause with no full stop yet: speak up to the comma now
        # rather than waiting for the whole sentence.
        if self.first and len(self.buf) >= self.first_clause_chars:
            i = max(self.buf.rfind(", "), self.buf.rfind("; "), self.buf.rfind(": "))
            if i >= 25:
                return i + 2
        return None


# What every model that writes something to be spoken is told (fast model, background agent, web search,
# external agents). clean_for_speech below catches what slips through anyway.
SPOKEN_STYLE = ("It's read aloud by a text-to-speech voice, so: no links, web addresses, citations or source "
                "lists, ever, not even at the end. If the user needs a website, say it in words, like \"on the "
                "Texas DMV's website\". No abbreviations a voice would misread: write out months and days "
                "(October 31, not Oct 31), units (miles per hour, degrees, minutes), and words like versus, for "
                "example, and so on, about. Numbers, dates and times the way a person says them.")


# Links, for something that's heard rather than read. Models (Haiku with web search especially) tack
# their sources on: "[1](https://…)", "[weather.gov](…)", "(weather.gov, accuweather.com)",
# "Sources: …", a bare address, a "[ESPN](…) | [NBA.com](…)" line. All of that goes. A site named
# in the middle of a sentence ("renew it at https://www.txdmv.gov/renew today") keeps just its name.
_CITATIONS = re.compile(r"\s*[(\[]\s*(\[[^\]]*\]\([^)]*\)[\s,;]*)+[)\]]")   # " ([nytimes.com](https://…))"
_MD_LINK = re.compile(r"\[([^\]]*)\]\(\s*<?([^)\s>]*)>?[^)]*\)")
_URL = re.compile(r"\b(?:https?://|www\.)[^\s<>()\[\]]*[^\s<>()\[\].,;:!?'\"]")
_DOMAIN = r"(?:[a-z0-9-]+\.)+[a-z]{2,6}(?:/[^\s,;)]*)?"
_DOMAIN_LIST = re.compile(rf"\s*\((?:\s*{_DOMAIN}\s*[,;]?)+\)", re.I)              # " (weather.gov, cnn.com)"
_SOURCES = re.compile(r"(?:^|(?<=[.!?]))[ \t]*\W*(sources?|references?|citations?|links?|read more|more info|"
                      r"learn more|via)\W*:[^\n]*", re.I | re.M)
_ONLY_LINKS = re.compile(rf"^[\s|,;·•\-–—()\[\]]*((\[[^\]]*\]\([^)]*\)|{_URL.pattern}|{_DOMAIN})"
                         rf"[\s|,;·•\-–—()\[\]]*)+$", re.I | re.M)
_TRAILING_DOMAINS = re.compile(rf"(?<=[.!?])(?:\s*[|,·•\-–]?\s*{_DOMAIN})+\s*$", re.I)
_FOOTNOTE = re.compile(r"\[\^?\d+\]|【[^】]*】")
_VAGUE = re.compile(r"\d+|source|sources|link|here|this|website|site|page|article|more", re.I)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def site_name(url: str) -> str:
    """"https://www.weather.gov/forecast?x=1" -> "weather.gov\""""
    return re.sub(r"^(https?://)?(www\.)?", "", url).split("/")[0].split("?")[0].split("#")[0]


def _link(m: re.Match) -> str:
    label, url = m.group(1).strip(), m.group(2)
    trailing = re.match(r"[\s.,;:!?)\]]*(?:\n|$)", m.string[m.end():])
    if not label or re.fullmatch(r"\d+", label) or (trailing and (_VAGUE.fullmatch(label) or _URL.fullmatch(label)
                                                                   or re.fullmatch(_DOMAIN, label, re.I))):
        return ""                                    # a citation, not words
    if _VAGUE.fullmatch(label) or _URL.fullmatch(label) or re.fullmatch(_DOMAIN, label, re.I):
        return website(site_name(url)) if url.startswith(("http", "www")) else ""   # "see [here](…) for more"
    return label


def _url(m: re.Match) -> str:
    rest = m.string[m.end():]
    # at the end of a line, alone or in a row of addresses: a source list, not directions
    if re.match(r"(?:[\s.,;:!?)\]|]*(?:https?://|www\.)\S+)*[\s.,;:!?)\]|]*(?:\n|$)", rest):
        return ""
    return website(site_name(m.group(0)))


def website(domain: str) -> str:
    """"txdmv.gov" -> "the TXDMV website", "weather.com" -> "the Weather website": a site said in words."""
    parts = domain.lower().split(".")
    name = parts[-2] if len(parts) >= 2 else parts[0]
    name = name.upper() if len(name) <= 4 or not re.search(r"[aeiouy]", name) else name.capitalize()
    return f"the {name} website"


_BARE_DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|gov|edu|io|co|us|uk|ai|app|tv|info|me)\b"
                          r"(?:/[^\s,;)]*)?", re.I)

# Abbreviations a voice reads wrong ("Oct" as "ock-t", "mph" as letters): spelled out
_MONTHS = {"jan": "January", "feb": "February", "mar": "March", "apr": "April", "jun": "June", "jul": "July",
           "aug": "August", "sep": "September", "sept": "September", "oct": "October", "nov": "November",
           "dec": "December"}
_DAYS = {"mon": "Monday", "tue": "Tuesday", "tues": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
         "thur": "Thursday", "thurs": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
_MONTH_RE = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)[a-z]*"
_UNITS = [(r"mph", "miles per hour"), (r"km/h|kph", "kilometers per hour"), (r"mins?", "minutes"),
          (r"hrs?", "hours"), (r"secs?", "seconds"), (r"km", "kilometers"), (r"mi", "miles"), (r"ft", "feet"),
          (r"lbs?", "pounds"), (r"oz", "ounces"), (r"kg", "kilograms"), (r"cm", "centimeters"), (r"mm", "millimeters")]
_WORDS = [(r"\be\.g\.,?", "for example,"), (r"\bi\.e\.,?", "that is,"), (r"\bvs\.?(?=\s)", "versus"),
          (r"\bapprox\.?(?=\s)", "about"), (r"\bw/(?=\s)", "with"), (r"\bMr\.(?=\s)", "Mister"),
          (r"\bMrs\.(?=\s)", "Missus"), (r"\bDr\.(?=\s+[A-Z])", "Doctor"), (r"\bJr\.", "Junior"),
          (r"\bNo\.(?=\s*\d)", "number"), (r"\s*&\s*", " and "), (r"(?<=\d)\s*%", " percent"),
          (r"\s*°\s*F\b", " degrees Fahrenheit"), (r"\s*°\s*C\b", " degrees Celsius"), (r"\s*°", " degrees")]


def spell_out(text: str) -> str:
    """Expand what a TTS voice would trip on: month and day abbreviations, units after numbers,
    "e.g.", "vs.", "etc." and friends."""
    text = re.sub(r"\b(Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?(?=\s+\d)",
                  lambda m: _MONTHS[m.group(1).lower()], text)
    text = re.sub(rf"\b(Mon|Tues?|Wed|Thu(?:rs?)?|Fri|Sat|Sun)\.?(?=,?\s+(?:{_MONTH_RE}|\d))",
                  lambda m: _DAYS[m.group(1).lower()], text)
    for abbr, word in _UNITS:
        text = re.sub(rf"(\d)\s*(?:{abbr})\b\.?", lambda m, w=word: f"{m.group(1)} {w}", text)
    text = re.sub(r"\b1 (minutes|hours|seconds|kilometers|miles|pounds|ounces|kilograms)\b",
                  lambda m: "1 " + m.group(1)[:-1], text)
    text = re.sub(r"\betc\.(?=\s*$|\s+[A-Z])", "and so on.", text)
    text = re.sub(r"\betc\.?", "and so on", text)
    for pat, word in _WORDS:
        text = re.sub(pat, " " + word if word[0].isalpha() and pat.startswith("\\s") else word, text)
    return text


def clean_for_speech(text: str) -> str:
    """Model output -> what's spoken and shown: no markdown, links, sources, footnotes or emoji. A
    list's lines become sentences, so the voice pauses between them."""
    text = _SOURCES.sub("", text)
    text = _CITATIONS.sub("", text)
    text = _ONLY_LINKS.sub("", text)
    text = _MD_LINK.sub(_link, text)
    text = _URL.sub(_url, text)
    text = _DOMAIN_LIST.sub("", text)
    text = _TRAILING_DOMAINS.sub("", text)
    text = _BARE_DOMAIN.sub(lambda m: website(m.group(0).split("/")[0]), text)   # "at dmv.org" -> "at the DMV website"
    text = spell_out(text)
    text = _FOOTNOTE.sub("", _EMOJI.sub("", text))
    text = re.sub(r"^\s*(#+|[-*•>]|\d+[.)])\s+", "", text, flags=re.M)
    text = re.sub(r"(\*\*|__|`|\*)", "", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip(" \t|·•-–—")]
    text = " ".join(ln if ln[-1] in ".!?:;,…" or i == len(lines) - 1 else ln + "." for i, ln in enumerate(lines))
    text = re.sub(r"\(\s*[,;]?\s*\)", "", re.sub(r"\s+", " ", text))                # "( )" left by removals
    text = re.sub(r"\s+([.,!?;:])", r"\1", text).strip()
    return _TRAILING_DOMAINS.sub("", text).strip(" |·•-–")


def synthesize(cloud, cfg, text: str, cancel: threading.Event | None = None):
    """Yields float32 48 kHz chunks of `text` as the TTS stream arrives, leading silence trimmed."""
    rs = dsp.StreamResampler(cfg.tts.sample_rate, HW_RATE)
    carry = b""
    leading = True
    for chunk in cloud.speech_stream(text, cancel):
        chunk = carry + chunk
        n = len(chunk) // 2 * 2
        carry = chunk[n:]
        x = np.frombuffer(chunk[:n], dtype="<i2").astype(np.float32) / 32768
        if leading:  # TTS engines pad the start with silence; skip it
            loud = np.flatnonzero(np.abs(x) > 0.004)
            if not len(loud):
                continue
            x = x[max(0, loud[0] - 240):]
            leading = False
        yield rs.process(x)


class TTSCache:
    """Fixed phrases ("On it.", "Timer set for 10 minutes.") synthesized once and
    kept on disk, so they play the instant they're needed instead of after a
    TTS round trip. Keyed by voice too, so changing [tts] never plays a stale voice."""

    def __init__(self, cfg, max_files: int = 400):
        self.cfg = cfg
        self.dir = cfg.path(cfg.system.state_dir) / "tts_cache"
        self.max_files = max_files

    def _file(self, text: str):
        import hashlib
        t = self.cfg.tts
        return self.dir / (hashlib.sha1(f"{t.model}|{t.voice}|{t.speed}|{text}".encode()).hexdigest()[:20] + ".npy")

    def get(self, text: str) -> np.ndarray | None:
        try:
            return np.load(self._file(text)).astype(np.float32) / 32767
        except (OSError, ValueError):
            return None

    def put(self, text: str, samples: np.ndarray) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            np.save(self._file(text), dsp.float_to_int16(samples))
            files = sorted(self.dir.glob("*.npy"), key=lambda p: p.stat().st_mtime)
            for old in files[:max(0, len(files) - self.max_files)]:
                old.unlink(missing_ok=True)
        except OSError:
            log.exception("tts cache: couldn't save")

    def warm(self, cloud, phrases) -> None:
        """Synthesize any of `phrases` not cached yet, in the background."""
        def go():
            for p in phrases:
                if self.get(p) is None:
                    try:
                        self.put(p, np.concatenate(list(synthesize(cloud, self.cfg, p)) or [np.zeros(0, np.float32)]))
                    except Exception as e:
                        log.debug("tts cache: warm-up of %r failed: %s", p, e)
                        return
        threading.Thread(target=go, name="tts-warm", daemon=True).start()


class SpeechSession:
    """One spoken reply. say() sentences as they arrive, then finish()."""

    def __init__(self, cloud, mixer, cfg, cancel: threading.Event, on_audio=None, cache: TTSCache | None = None,
                 on_caption=None):
        """on_caption(text): the sentence now being heard, as playback reaches it (closed captions)."""
        self.cloud, self.mixer, self.cfg, self.cancel = cloud, mixer, cfg, cancel
        self.cache = cache
        src = StreamSource(on_audio=on_audio)
        src.on_mark = on_caption
        self.source = mixer.play(src)
        self._slots = threading.Semaphore(2)   # sentences synthesizing at once
        self._segments: queue.Queue = queue.Queue()
        self._sequencer = threading.Thread(target=self._sequence, name="tts-seq", daemon=True)
        self._sequencer.start()
        self.error: Exception | None = None
        self._wake_word = (cfg.wake.keyword or cfg.assistant.name).lower()
        self.says_wake_word = False         # e.g. "Okay, back to Franklin." (the mic will hear it)
        self.finished_at = float("inf")     # when the last sample was handed to the mixer

    def say(self, text: str, cache: bool = False) -> None:
        """cache=True for fixed phrases: played from the TTS cache, synthesized (and kept) on a miss."""
        text = clean_for_speech(text)
        if not text or self.cancel.is_set():
            return
        if self._wake_word in text.lower():
            self.says_wake_word = True
        cached = self.cache.get(text) if cache and self.cache else None
        if cached is not None:
            self.play(cached, text)
            return
        seg: queue.Queue = queue.Queue()
        self._segments.put((seg, text))
        keep = self.cache if cache else None
        threading.Thread(target=self._fetch, args=(text, seg, keep), name="tts", daemon=True).start()

    def play(self, samples: np.ndarray, text: str | None = None) -> None:
        """Insert a pre-rendered sound (e.g. a chime, or a cached phrase saying `text`) in sequence."""
        seg: queue.Queue = queue.Queue()
        seg.put(samples)
        seg.put(None)
        self._segments.put((seg, text))

    def finish(self, wait: bool = True) -> None:
        self._segments.put(None)
        if wait:
            self.wait()

    def wait(self) -> None:
        while not self.source.done.wait(0.1):
            if self.cancel.is_set():
                self.abort()
                return
        self.finished_at = time.monotonic()

    def abort(self) -> None:
        self.cancel.set()
        self.source.stop()
        self._segments.put(None)

    def _fetch(self, text: str, seg: queue.Queue, keep: TTSCache | None = None) -> None:
        got = []
        try:
            self._slots.acquire()
            if self.cancel.is_set():
                return
            for attempt in (1, 2):
                try:
                    t0 = time.monotonic()
                    for x in synthesize(self.cloud, self.cfg, text, self.cancel):
                        if not got:
                            log.info("tts: first audio after %.2fs (%d chars)", time.monotonic() - t0, len(text))
                        seg.put(x)
                        got.append(x)
                    if got or self.cancel.is_set():
                        break
                    raise RuntimeError("the voice provider sent no audio")
                except Exception as e:
                    # a provider hiccup before any sound: try once more rather than lose the reply
                    if got or self.cancel.is_set() or attempt == 2:
                        raise
                    log.warning("tts: %s; retrying", e)
            if keep is not None and got and not self.cancel.is_set():
                keep.put(text, np.concatenate(got))
        except Exception as e:
            log.error("tts: %s", e)
            self.error = e
        finally:
            self._slots.release()
            seg.put(None)

    def _sequence(self) -> None:
        while True:
            item = self._segments.get()
            if item is None or self.cancel.is_set():
                break
            seg, text = item
            first = True
            while True:
                x = seg.get()
                if x is None or self.cancel.is_set():
                    break
                if first and text:
                    self.source.mark(text)       # the screen shows it when it's actually heard
                first = False
                self.source.push(x)
        self.source.close()
