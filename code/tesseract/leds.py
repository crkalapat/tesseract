"""WS2812B strip animations.

Design language (same colours as the display accents):
  idle         dark (optional faint breathing); red glow while muted
  wake word    a quick bloom out from the centre, then...
  listening    cyan, swelling from the centre with your voice
  transcribing cyan->blue ripple running along the strip
  thinking     soft violet light gliding back and forth
  working      the same in gold (tool/agent calls in progress)
  speaking     cool white that breathes with Franklin's actual voice
  error        two red blinks, then fades
  alarm/timer  warm orange pulse
  volume       white bar for a moment

When idle, the strip shows what's going on in the background, most urgent first:
  needs you    an amber light running down the strip (a job asked you something)
  failed       slow red pulse
  done         slow green pulse (also tints the voice green while it reads the result)
  working      violet progress bar for the background job, shimmering at its edge
  timer        a dim warm bar that drains as the soonest timer runs down

Everything renders as float RGB in 0..1 at 50 fps, cross-fades between states,
then gets gamma-corrected and brightness-capped (lower at night) once.
"""

from __future__ import annotations

import logging
import math
import threading
import time

import numpy as np

from .splash import open_strip
from .state import State
from .util import blend, darkness

log = logging.getLogger(__name__)

CYAN = np.array([0.0, 0.75, 1.0])
BLUE = np.array([0.1, 0.3, 1.0])
VIOLET = np.array([0.5, 0.25, 1.0])
GOLD = np.array([1.0, 0.6, 0.1])
WHITE = np.array([0.75, 0.88, 1.0])
RED = np.array([1.0, 0.04, 0.02])
ORANGE = np.array([1.0, 0.35, 0.05])
GREEN = np.array([0.1, 1.0, 0.3])
WARM = np.array([1.0, 0.7, 0.4])
AMBER = np.array([1.0, 0.5, 0.0])
TINTS = {"done": GREEN, "failed": RED, "waiting": AMBER}

FPS = 50


def _smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def to_pixels(frame: np.ndarray, cap: float) -> np.ndarray:
    """0..1 linear colours -> 0..255 LED values: gamma first, then the brightness cap."""
    out = (np.power(frame, 2.2) * (cap * 255) + 0.5).astype(np.uint8)
    # gamma crushes dim colours to zero at night: keep anything meant to be visible at least barely lit
    out[(out == 0) & (frame > 0.05)] = 1
    return out


class Ws281xStrip:
    def __init__(self, cfg):
        self.strip = open_strip(cfg)

    def show(self, rgb: np.ndarray) -> None:
        for i, (r, g, b) in enumerate(rgb.tolist()):
            self.strip.setPixelColor(i, (r << 16) | (g << 8) | b)
        self.strip.show()


class MemoryStrip:
    """Keeps the last frame (the simulator draws it under the virtual display)."""
    def __init__(self, n):
        self.frame = np.zeros((n, 3), dtype=np.uint8)

    def show(self, rgb):
        self.frame = rgb


class Leds:
    def __init__(self, cfg, status, mic_level=lambda: 0.0, speaker_level=lambda: 0.0, sim=False,
                 continuing: bool = False):
        """continuing: the boot light (splash.py) was showing the "Starting" rainbow: carry it on
        rather than fading in from black."""
        self.cfg = cfg
        self.status = status
        self.mic_level = mic_level
        self.speaker_level = speaker_level
        n = cfg.leds.count
        self.x = np.linspace(0, 1, n) if n > 1 else np.array([0.5])
        if cfg.leds.reverse:
            self.x = self.x[::-1]
        self.d = np.abs(self.x - 0.5) * 2          # 0 at the centre, 1 at the ends
        self.strip = MemoryStrip(n)
        if not sim and cfg.leds.enabled:
            try:
                self.strip = Ws281xStrip(cfg)
            except Exception as e:
                log.warning("leds: disabled (%s)", e)
        self._prev = np.zeros((n, 3))
        self._from = np.zeros((n, 3))
        self._continuing = continuing
        self._last_state = (State.BOOT, "") if continuing else None
        self._fade_start = 0.0
        self._lvl = 0.0
        self._night = 0.0           # 0 = day .. 1 = night (see util.darkness)
        self._night_checked = 0.0
        self.running = True

    def start(self) -> None:
        self.running = True
        threading.Thread(target=self._run, name="leds", daemon=True).start()

    def off(self) -> None:
        self.running = False
        time.sleep(0.05)
        self.strip.show(np.zeros((len(self.x), 3), dtype=np.uint8))

    # ── scenes ──────────────────────────────────────────────────────────────
    def _scene(self, s, t: float, age: float) -> np.ndarray:
        x, d = self.x[:, None], self.d[:, None]
        st = s.state

        if s.power:                    # shutting down: fade out
            return np.zeros((len(self.x), 3))

        if st == State.BOOT:       # the boot light's rainbow (splash.py), on the same clock so it carries on
            hue = (self.x[:, None] + time.monotonic() * 0.5 + np.array([0, 1 / 3, 2 / 3])) % 1.0
            return (0.5 + 0.5 * np.cos(2 * math.pi * hue)) * 0.7 * (1.0 if self._continuing else min(1.0, age * 2))

        if st == State.LISTENING:
            target = self.mic_level()
            self._lvl += (0.5 if target > self._lvl else 0.08) * (target - self._lvl)
            if age < 0.4:  # wake bloom
                r = _smoothstep(age / 0.3) * 1.2
                bloom = _smoothstep((r - d) * 4)
                col = WHITE + (CYAN - WHITE) * _smoothstep(age / 0.4)
                return bloom * col
            swell = _smoothstep((0.12 + self._lvl * 0.95 - d) * 3)
            return CYAN * (0.16 + 0.84 * swell * (0.3 + 0.7 * self._lvl) + 0.04 * math.sin(t * 3))

        if st == State.TRANSCRIBING:
            w = 0.5 + 0.5 * np.sin(2 * math.pi * (x * 1.2 - t * 1.8))
            return (CYAN + (BLUE - CYAN) * w) * (0.25 + 0.75 * w ** 2)

        if st in (State.THINKING, State.WORKING):
            col = GOLD if st == State.WORKING or s.agent_mode else VIOLET
            pos = 0.5 + 0.55 * math.sin(t * 2.6)
            blob = np.exp(-((x - pos) / 0.22) ** 2)
            pos2 = 0.5 + 0.55 * math.sin(t * 2.6 - 0.9)   # faint trailing echo
            trail = 0.35 * np.exp(-((x - pos2) / 0.3) ** 2)
            return col * (0.08 + 0.9 * np.maximum(blob, trail))

        if st == State.SPEAKING:
            target = self.speaker_level()
            self._lvl += (0.6 if target > self._lvl else 0.15) * (target - self._lvl)
            r = 0.15 + 0.85 * self._lvl
            body = _smoothstep((r - d) * 3)
            col = WHITE
            if s.tint in TINTS:
                col = TINTS[s.tint] * 0.7 + WHITE * 0.3
            elif s.agent_mode:
                col = GOLD * 0.5 + WHITE * 0.5
            return col * (0.12 + 0.88 * body * (0.35 + 0.65 * self._lvl))

        if st == State.ERROR:
            if age < 0.8:
                on = (age % 0.4) < 0.2
                return RED * (0.9 if on else 0.0) * np.ones_like(x)
            return RED * max(0.0, 1 - (age - 0.8)) * 0.6 * np.ones_like(x)

        if st == State.ALARM:
            p = math.sin(math.pi * t * (1.6 if "timer" in s.detail.lower() else 1.0)) ** 2
            col = GOLD if "timer" in s.detail.lower() else ORANGE
            return col * (0.15 + 0.85 * p) * np.ones_like(x)

        # IDLE
        out = np.zeros((len(self.x), 3))
        if s.muted:
            out[:] = RED * 0.25      # privacy first: muted always shows
        else:
            bg = self._background(s, t)
            if bg is not None:
                out = bg
            elif self.cfg.leds.idle_glow:
                out[:] = WARM * (0.03 + 0.02 * math.sin(t * 2 * math.pi / 8))
        if not s.online:
            out[0] = GOLD * (0.1 + 0.25 * (0.5 + 0.5 * math.sin(t * 2)))
        return out

    def _background(self, s, t: float) -> np.ndarray | None:
        """What a background job or timer looks like while nothing else is happening."""
        x, n = self.x[:, None], len(self.x)
        breathe = 0.5 - 0.5 * math.cos(2 * math.pi * t / 3.0)       # slow: one breath every 3 s
        if s.task_state == "waiting" or s.setup:                     # a light running down the strip: needs you
            pos = (t * 0.7) % 1.4 - 0.2
            return AMBER * (0.04 + 0.6 * np.exp(-((x - pos) / 0.12) ** 2))
        if s.task_state == "failed":
            return RED * (0.12 + 0.4 * breathe) * np.ones_like(x)
        if s.task_state in ("done", "ready"):
            return GREEN * (0.1 + 0.45 * breathe) * np.ones_like(x)
        if s.task_state == "running":
            lit = _smoothstep((s.task_progress - x) * n + 0.5)
            edge = np.exp(-((x - s.task_progress) * n / 1.2) ** 2) * (0.5 + 0.5 * math.sin(t * 5))
            return VIOLET * (0.03 + 0.4 * lit + 0.3 * edge * lit)
        if s.timers:
            label, due, total = s.timers[0]
            left = max(0.0, due - time.time())
            frac = left / total if total > 0 else 0.0
            lit = _smoothstep((frac - x) * n + 0.5)
            level = 0.32
            if left < 10:                                            # last ten seconds: a gentle pulse
                level *= 0.6 + 0.4 * (0.5 + 0.5 * math.sin(t * 2 * math.pi))
            return WARM * level * lit
        return None

    def _overlays(self, s, frame: np.ndarray, now: float) -> np.ndarray:
        # volume bar
        age = now - s.volume_changed
        if age < 1.5:
            fill = s.volume / 10
            lit = _smoothstep((fill - self.x) * len(self.x) + 0.5)[:, None]
            bar = WHITE * (0.04 + 0.7 * lit)
            k = 1.0 if age < 1.2 else 1 - (age - 1.2) / 0.3
            frame = frame * (1 - k) + bar * k
        return frame

    def _run(self) -> None:
        period = 1 / FPS
        t0 = time.monotonic()
        nxt = t0
        while self.running:
            now = time.monotonic()
            t = now - t0
            s = self.status
            if (s.state, s.power) != self._last_state:
                self._from = self._prev.copy()
                self._fade_start = now
                self._last_state = (s.state, s.power)
            try:
                frame = np.clip(self._scene(s, t, s.age()), 0, 1)
                fade = min(1.0, (now - self._fade_start) / (1.5 if s.power else 0.25))
                if fade < 1 and s.state != State.LISTENING:  # the bloom has its own fade-in
                    frame = self._from + (frame - self._from) * fade
                frame = np.clip(self._overlays(s, frame, now), 0, 1)
            except Exception:
                log.exception("leds: render failed")
                frame = np.zeros_like(self._prev)
            self._prev = frame

            if now - self._night_checked > 5:
                self._night = darkness(self.cfg, None, s.night_until, s.day_until)
                self._night_checked = now
            cap = blend(self.cfg.leds.brightness, self.cfg.leds.night_brightness, self._night)
            out = to_pixels(frame, cap)
            try:
                self.strip.show(out)
            except Exception:
                log.exception("leds: show failed")
            nxt += period
            time.sleep(max(0.0, nxt - time.monotonic()))
            if nxt < time.monotonic() - 1:
                nxt = time.monotonic()
