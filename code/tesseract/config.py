"""Loads config.toml + .env into typed dataclasses.

Defaults live here so a missing key in config.toml never crashes the speaker;
config.toml documents what each setting does.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

CODE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Pins:
    button_action: int = 26
    button_vol_up: int = 22
    button_vol_down: int = 5
    button_mute: int = 6
    led_data: int = 12
    display_dc: int = 27
    display_reset: int = 17
    display_spi_bus: int = 0
    display_spi_device: int = 0
    display_cs: int = 8
    amp_enable_dummy: int = 25
    amp_enable: int = 0          # the amp's SD_MODE on a GPIO (0 = hard-wired on)


@dataclass
class Assistant:
    name: str = "Franklin"
    location: str = ""
    units: str = "imperial"
    extra_instructions: str = ""
    memory_minutes: float = 5
    max_history_turns: int = 8
    follow_up: bool = True
    follow_up_seconds: float = 6


@dataclass
class Wake:
    engine: str = "both"
    keyword: str = ""
    keyword_threshold: float = 0.3
    keyword_boost: float = 0.5
    # other ways the name comes out when said quickly; "word#0.45" = a stricter threshold for that one
    keyword_variants: list[str] = field(default_factory=lambda: ["Franken", "Franklyn", "Frankin#0.45"])
    model: str = "models/franklin.onnx"
    fallback_model: str = "hey_jarvis"
    threshold: float = 0.6
    frames: int = 2
    porcupine_keyword: str = "models/franklin_raspberry-pi.ppn"
    porcupine_sensitivity: float = 0.6
    speaking_threshold_boost: float = 0.1


@dataclass
class Audio:
    device: str = "auto"
    mic_channel: int = 0
    mic_gain_db: float = 20
    volume: int = 6
    max_volume_db: float = 0
    echo_cancel: bool = True
    echo_suppress: float = 2.0


@dataclass
class Listen:
    early_silence_ms: int = 200
    complete_silence_ms: int = 750
    end_silence_ms: int = 1200
    start_timeout_s: float = 5
    max_utterance_s: float = 60
    long_talk_after_s: float = 6     # once you've talked this long, you're explaining something:
    long_pause_ms: int = 1600        # ...a pause has to be this long to count as the end
    preroll_ms: int = 300
    vad_aggressiveness: int = 2
    vad_threshold: float = 0.5


@dataclass
class STT:
    model: str = "openai/whisper-large-v3-turbo"
    language: str = "en"
    provider_order: list[str] = field(default_factory=lambda: ["groq"])


@dataclass
class LLM:
    """The fast model: answers, timers, and deciding what goes to [thinking]."""
    model: str = "openai/gpt-oss-120b"
    reasoning: str = "low"
    max_tokens: int = 300
    temperature: float | None = None
    provider_order: list[str] = field(default_factory=lambda: ["cerebras", "groq", "sambanova"])
    provider_sort: str = "latency"
    search_model: str = "openai/gpt-oss-120b"


@dataclass
class Thinking(LLM):
    """The background agent's model, for multi-step jobs."""
    model: str = "anthropic/claude-haiku-5.5"
    reasoning: str = "low"
    max_tokens: int = 2000
    provider_order: list[str] = field(default_factory=list)
    provider_sort: str = ""
    max_steps: int = 12
    input_timeout_s: float = 600
    keep_builtin: bool = False      # with [[agents]] too: keep the on-device calendar/to-do agent as a choice


@dataclass
class TTS:
    model: str = "microsoft/mai-voice-2.1-flash"
    voice: str = "en-US-Grant:MAI-Voice-2.1-Flash"
    sample_rate: int = 24000
    speed: float = 1.0


@dataclass
class Leds:
    enabled: bool = True
    count: int = 9
    brightness: float = 0.35
    night_brightness: float = 0.04
    idle_glow: bool = False
    reverse: bool = False
    dma: int = 13
    strip_type: str = "GRB"


@dataclass
class Display:
    spi_hz: int = 10_000_000
    rotation: int = 0
    mirror: str = "none"
    color_order: str = "RGB"
    clock_24h: bool = False
    brightness: int = 10
    night_brightness: int = 1
    burn_in_shift: bool = True


@dataclass
class Night:
    start: str = "02:00"
    sunrise_start: str = "06:30"
    sunrise_end: str = "07:30"


@dataclass
class Sounds:
    wake_chime: bool = True
    volume: float = 0.5


@dataclass
class Network:
    setup_hotspot: bool = True
    setup_after_s: float = 45
    hotspot_name: str = "Tesseract-Setup"


@dataclass
class System:
    state_dir: str = "data"
    log_level: str = "INFO"


@dataclass
class AgentConfig:
    name: str
    type: str
    description: str = ""
    url: str = ""
    api_key_env: str = ""
    model: str = ""
    session: str = "tesseract"
    new_conversation_after_min: float = 15   # Hermes: one ongoing conversation, fresh after this long idle; 0 = every job alone
    reasoning_effort: str = "low"  # Hermes: per-request model_options; "" = Hermes's own setting. none..high
    service_tier: str = ""         # Hermes: "priority"/"fast" = faster, but uses the ChatGPT plan's limit faster
    command: list[str] = field(default_factory=list)
    timeout_s: float = 600

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""


@dataclass
class Spotify:
    enabled: bool = False         # `tesseract spotify setup` turns it on
    device_name: str = "Tesseract"
    soloist: str = ""             # Spotify Soloist, the player; "" = the copy setup keeps in data/soloist
    ws_port: int = 47821          # Soloist's control socket (127.0.0.1 only)
    volume: float = 0.6           # music next to Franklin's voice (1 = as loud)
    duck: float = 0.15            # music level while you talk to Franklin


@dataclass
class LocalLLM:
    """A model on your own computer (LM Studio, Ollama, llama.cpp: anything OpenAI-compatible)."""
    enabled: bool = False
    url: str = ""                 # e.g. http://my-macbook.local:1234/v1
    model: str = ""               # as the server lists it, e.g. qwen/qwen3.5-9b
    api_key_env: str = ""         # LM Studio and Ollama need none
    use_for: str = "fast"         # "fast": answers and routing; "all": the background agent too
    fallback: bool = True         # computer asleep or out of range: use the cloud models instead
    max_tokens: int = 400

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""


@dataclass
class Config:
    pins: Pins = field(default_factory=Pins)
    assistant: Assistant = field(default_factory=Assistant)
    wake: Wake = field(default_factory=Wake)
    audio: Audio = field(default_factory=Audio)
    listen: Listen = field(default_factory=Listen)
    stt: STT = field(default_factory=STT)
    llm: LLM = field(default_factory=LLM)
    thinking: Thinking = field(default_factory=Thinking)
    tts: TTS = field(default_factory=TTS)
    leds: Leds = field(default_factory=Leds)
    display: Display = field(default_factory=Display)
    night: Night = field(default_factory=Night)
    sounds: Sounds = field(default_factory=Sounds)
    network: Network = field(default_factory=Network)
    system: System = field(default_factory=System)
    local_llm: LocalLLM = field(default_factory=LocalLLM)
    spotify: Spotify = field(default_factory=Spotify)
    agents: list[AgentConfig] = field(default_factory=list)
    base_dir: Path = CODE_DIR
    source: Path = CODE_DIR / "config.toml"    # the file this was loaded from
    local: Path = CODE_DIR / "data" / "local.toml"   # settings changed on the device (settings.py writes here)

    @property
    def openrouter_key(self) -> str:
        return os.environ.get("OPENROUTER_API_KEY", "")

    def path(self, p: str) -> Path:
        """Resolve a path from config relative to the config file's folder."""
        q = Path(p).expanduser()
        return q if q.is_absolute() else self.base_dir / q


def _build(cls, data: dict, section: str):
    known = {f.name for f in dataclasses.fields(cls)}
    for k in data:
        if k not in known:
            log.warning("config: unknown key [%s] %s (typo?)", section, k)
    return cls(**{k: v for k, v in data.items() if k in known})


def load_env(path: Path) -> None:
    """Minimal .env reader (KEY=value lines). Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def load(path: str | Path | None = None) -> Config:
    path = Path(path) if path else CODE_DIR / "config.toml"
    load_env(path.parent / ".env")
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    if not path.exists():
        log.warning("config: %s not found, using defaults", path)
    # Settings changed on the device itself ("Franklin, I'm in Chicago") live in data/local.toml,
    # layered on top, so copying a new config.toml over from a laptop doesn't wipe them.
    local = path.parent / raw.get("system", {}).get("state_dir", "data") / "local.toml"
    if local.exists():
        try:
            for section, values in tomllib.loads(local.read_text()).items():
                if section == "agents":      # agents added on the device replace same-named ones
                    names = {a.get("name") for a in values}
                    raw["agents"] = [a for a in raw.get("agents", []) if a.get("name") not in names] + values
                else:
                    raw.setdefault(section, {}).update(values)
        except (OSError, tomllib.TOMLDecodeError) as e:
            log.warning("config: ignoring %s (%s)", local, e)

    sections = {f.name: f.type for f in dataclasses.fields(Config)}
    kwargs = {}
    for name, cls_name in sections.items():
        if name in ("agents", "base_dir", "source", "local") or name not in raw:
            continue
        cls = globals()[cls_name] if isinstance(cls_name, str) else cls_name
        kwargs[name] = _build(cls, raw[name], name)
    kwargs["agents"] = [_build(AgentConfig, a, "agents") for a in raw.get("agents", [])]
    for k in raw:
        if k not in sections:
            log.warning("config: unknown section [%s]", k)
    return Config(**kwargs, base_dir=path.parent.resolve(), source=path, local=local)
