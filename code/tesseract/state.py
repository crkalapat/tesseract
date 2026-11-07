"""The shared picture of what Tesseract is doing; read by the LED and display threads."""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass, field


class State(str, enum.Enum):
    BOOT = "boot"
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    WORKING = "working"        # a tool / agent call is in progress mid-reply
    SPEAKING = "speaking"
    ERROR = "error"
    ALARM = "alarm"


LABELS = {
    State.BOOT: "Starting",
    State.IDLE: "",
    State.LISTENING: "Listening",
    State.TRANSCRIBING: "Transcribing",
    State.THINKING: "Thinking",
    State.WORKING: "Working",
    State.SPEAKING: "Speaking",
    State.ERROR: "Error",
    State.ALARM: "Alarm",
}


@dataclass
class Status:
    state: State = State.BOOT
    since: float = field(default_factory=time.monotonic)
    detail: str = ""            # e.g. "Asking Hermes" or an error message
    user_text: str = ""         # what you said
    reply_text: str = ""        # what Franklin is saying
    agent_mode: str = ""        # name of the agent we're talking to directly
    muted: bool = False
    quiet: bool = False         # quiet mode: job results wait (green light) until you ask for them
    volume: int = 6
    screen: int = 10            # display brightness 0-10 (0 = off)
    night_until: float = 0.0    # "good night": night mode until this unix time (the next sunrise_end)
    day_until: float = 0.0      # "good morning": no night mode until this unix time
    volume_changed: float = 0.0
    online: bool = True
    next_alarm: str = ""
    timers: list[tuple[str, float, float]] = field(default_factory=list)  # (label, due unix time, length s), soonest first
    # the background job the LEDs/display show: "" | "running" | "waiting" (for your answer) | "done" | "failed"
    # | "ready" (quiet mode: a result is waiting for you to ask for it)
    task_state: str = ""
    task_progress: float = 0.0
    task_label: str = ""        # e.g. "Checking your calendar"
    caption: str = ""           # the sentence being spoken right now (closed captions while SPEAKING)
    caption_at: float = 0.0     # when it started (monotonic), to scroll a long one
    tint: str = ""              # colours the speaking animation while it reads out job news: a task_state
    setup: dict | None = None   # Wi-Fi setup in progress: {ssid, password, url, error} or {connecting}
    power: str = ""             # "off" / "restart": shutting down
    music: dict | None = None   # Spotify's now playing: {title, artist}
    version: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set(self, **kw) -> None:
        with self._lock:
            if "state" in kw and kw["state"] != self.state:
                self.since = time.monotonic()
            for k, v in kw.items():
                setattr(self, k, v)
            self.version += 1

    def age(self) -> float:
        return time.monotonic() - self.since
