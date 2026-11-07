"""NHD-1.91-176176UBC3 (SSD1333 controller, 176x176 RGB565 over SPI) + the UI.

Idle:   the date in grey over a large, light clock (no AM/PM), timers counting down
        under it, and a quiet row at the bottom (next alarm, mic off, background job).
Active: the time small at the top, what Tesseract is doing ("Listening", "Thinking",
        ...) with a live indicator, and the words: yours, then Franklin's reply.
Night:  dimmer and warm red after "good night", easing back from 6:30 to 7:30.

Only the rectangle of pixels that changed since the last frame is sent over
SPI, so a spinner costs a few KB per frame instead of 62 KB.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import re
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .state import LABELS, State
from .util import blend, darkness, format_clock

log = logging.getLogger(__name__)

from .panel import GAMMA, H, SSD1333, W  # noqa: F401  (the driver lives in panel.py: no numpy, for the boot light)


class SimPanel:
    """--sim: writes the screen (and the LED strip under it) to a PNG."""

    def __init__(self, path: Path, leds=None):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.leds = leds
        self._t = 0.0
        self.level = 15

    def brightness(self, level):
        self.level = level

    def power(self, on):
        pass

    def show(self, img: Image.Image) -> None:
        now = time.monotonic()
        if now - self._t < 0.2:
            return
        self._t = now
        scale = 2
        out = Image.new("RGB", (W * scale + 40, H * scale + 80), (24, 24, 28))
        dim = img.point(lambda v: int(v * (0.35 + 0.65 * self.level / 15)))
        out.paste(dim.resize((W * scale, H * scale), Image.NEAREST), (20, 20))
        if self.leds is not None:
            frame = getattr(self.leds.strip, "frame", None)
            if frame is not None and len(frame):
                d = ImageDraw.Draw(out)
                n = len(frame)
                cw = (W * scale) / n
                for i, (r, g, b) in enumerate(frame.tolist()):
                    # undo gamma so the preview looks like the diffused LED
                    c = tuple(int(255 * (v / 255) ** (1 / 2.2)) for v in (r, g, b))
                    d.rounded_rectangle([20 + i * cw + 3, H * scale + 35, 20 + (i + 1) * cw - 3, H * scale + 60],
                                        radius=6, fill=c)
        tmp = self.path.with_suffix(".tmp.png")
        out.save(tmp)
        tmp.replace(self.path)

    def close(self):
        pass


# ── UI ──────────────────────────────────────────────────────────────────────
# A quiet, minimal look: black, a soft white for what matters, grey for the rest, and one
# muted accent colour per state. Everything is set in Inter (code/fonts, SIL OFL), with
# Inter Display Light for the big clock.

ACCENT = {
    State.BOOT: (200, 202, 210),
    State.LISTENING: (90, 200, 255),
    State.TRANSCRIBING: (110, 160, 255),
    State.THINKING: (165, 140, 255),
    State.WORKING: (240, 180, 90),
    State.SPEAKING: (236, 238, 242),
    State.ERROR: (255, 110, 100),
    State.ALARM: (255, 150, 80),
}
DAY = {"fg": (236, 238, 242), "dim": (128, 133, 143), "faint": (70, 74, 82)}
NIGHT = {"fg": (200, 74, 40), "dim": (118, 44, 24), "faint": (64, 24, 12)}   # warm, for the dark
GOLD = (240, 180, 90)
TIMER = (255, 160, 90)
RED = (240, 90, 80)
# status-row item per job state: (icon, text, colour); icons are drawn, since the fonts lack ✓ ✕
TASK = {"running": ("bar", "Working", (165, 140, 255)), "waiting": ("dot", "Needs you", (255, 160, 70)),
        "done": ("check", "Done", (90, 215, 130)), "ready": ("check", "Ready", (90, 215, 130)),
        "failed": ("cross", "Failed", RED)}
# the middle of the screen, where Listening / Thinking / a ringing timer sit (label, then indicator)
CENTER_Y, INDICATOR_Y = 80, 104


def mix(a, b, k: float) -> tuple:
    return tuple(round(x + (y - x) * k) for x, y in zip(a, b))


def countdown(seconds: float) -> str:
    s = max(0, int(math.ceil(seconds)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_FONT_DIRS = [Path(__file__).resolve().parent.parent / "fonts", Path("/usr/share/fonts/truetype/dejavu"),
              Path("/usr/share/fonts/TTF"), Path("/Library/Fonts"), Path("/System/Library/Fonts/Supplemental")]
_FONT_FILES = {   # first one found wins; DejaVu/Arial only if code/fonts is missing
    "display": ["InterDisplay-Light.ttf", "Inter-Light.ttf", "DejaVuSans-ExtraLight.ttf", "Arial.ttf"],
    "light": ["Inter-Light.ttf", "DejaVuSans-ExtraLight.ttf", "Arial.ttf"],
    "regular": ["Inter-Regular.ttf", "DejaVuSans.ttf", "Arial.ttf"],
    "medium": ["Inter-Medium.ttf", "DejaVuSans.ttf", "Arial.ttf"],
}
_font_cache: dict = {}


def font(size: int, weight: str = "regular"):
    key = (size, weight)
    if key not in _font_cache:
        f = None
        for name in _FONT_FILES[weight]:
            p = next((d / name for d in _FONT_DIRS if (d / name).exists()), None)
            if p:
                f = ImageFont.truetype(str(p), size)
                break
        _font_cache[key] = f or ImageFont.load_default(size)
    return _font_cache[key]


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, width: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            trial = f"{line} {word}".strip()
            if draw.textlength(trial, font=fnt) <= width:
                line = trial
            else:
                if line:
                    lines.append(line)
                line = word
        if line:
            lines.append(line)
    return lines


def panel_levels(fraction: float) -> tuple[int, float]:
    """Screen brightness 0..1 -> the panel's master current step (0-15, 16 coarse steps) and a
    pixel scale for the rest, so the sunrise ramp is smooth rather than stepping."""
    step = max(0, min(15, math.ceil(fraction * 16) - 1))
    return step, min(1.0, fraction / ((step + 1) / 16))


# a slow orbit of a few pixels, one step per minute (OLED burn-in protection)
_ORBIT = [(0, 0), (2, 1), (3, 3), (2, 5), (0, 6), (-2, 5), (-3, 3), (-2, 1)]


class Display:
    def __init__(self, cfg, status, mic_level=lambda: 0.0, speaker_level=lambda: 0.0,
                 sim: bool = False, leds=None):
        self.cfg = cfg
        self.status = status
        self.mic_level = mic_level
        self.speaker_level = speaker_level
        self.panel = None
        if sim:
            self.panel = SimPanel(cfg.path(cfg.system.state_dir) / "sim.png", leds)
        else:
            try:
                self.panel = SSD1333(cfg)
            except Exception as e:
                log.warning("display: disabled (%s)", e)
        self.running = True
        self._bars = np.zeros(7)
        self._clock_sizes: dict[str, int] = {}

    def start(self) -> None:
        if self.panel is not None:
            threading.Thread(target=self._run, name="display", daemon=True).start()

    def stop(self) -> None:
        self.running = False
        if self.panel is not None:
            time.sleep(0.1)
            self.panel.close()

    def fraction(self, s, now: dt.datetime) -> tuple[float, float]:
        """(screen brightness 0..1, darkness 0..1) right now."""
        k = darkness(self.cfg, now, s.night_until, s.day_until)
        if s.screen <= 0:
            return 0.0, k
        return blend(s.screen / 10, max(0.02, self.cfg.display.night_brightness / 10), k), k

    def _run(self) -> None:
        last_key, last_step, last_on = None, None, None
        while self.running:
            s = self.status
            now = dt.datetime.now()
            frac, k = self.fraction(s, now)
            on = frac > 0
            if on != last_on:
                self.panel.power(on)
                last_on = on
            step, scale = panel_levels(frac)
            if on and step != last_step:
                self.panel.brightness(step)
                last_step = step
            animating = s.state != State.IDLE
            # while a timer counts down (or a job's bar moves), the idle screen changes every second
            tick = now.second if (s.timers or s.task_state == "running") else 0
            key = (s.version, now.minute, tick, round(k * 30), round(scale, 2))
            if on and (animating or key != last_key):
                t0 = time.monotonic()
                try:
                    img = self.render(s, now, k)
                    if scale < 0.99:
                        img = img.point(lambda v: int(v * scale))
                    self.panel.show(img)
                except Exception:
                    log.exception("display: render failed")
                took = time.monotonic() - t0
                if last_key is None:
                    log.info("display: first frame sent (%.0f ms)", took * 1000)
                elif took > 1:
                    log.warning("display: a frame took %.1fs to send (is the SPI bus stuck?)", took)
                last_key = key
            time.sleep(1 / 15 if animating else 0.1)

    # ── drawing ─────────────────────────────────────────────────────────────
    def render(self, s, now: dt.datetime, dark: float = 0.0) -> Image.Image:
        """dark: 0 = day colours .. 1 = night colours (warm, for the dark)."""
        img = Image.new("RGB", (W, H), (0, 0, 0))
        d = ImageDraw.Draw(img)
        pal = {k: mix(DAY[k], NIGHT[k], dark) for k in DAY}
        if s.power:
            self._power(d, s, pal)
        elif s.setup and s.state == State.IDLE:
            self._setup(img, d, s.setup)
        elif s.state == State.IDLE:
            self._idle(d, s, now, pal, dark)
        elif s.state == State.ALARM:
            self._alarm(d, s, now, pal, dark)
        else:
            self._active(d, s, now, pal, dark)
        return self._orient(img)

    def _orient(self, img: Image.Image) -> Image.Image:
        rot = self.cfg.display.rotation % 360
        if rot:
            img = img.rotate(-rot)
        mirror = self.cfg.display.mirror.lower()
        if mirror in ("horizontal", "both"):
            img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if mirror in ("vertical", "both"):
            img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        return img

    def splash_frame(self) -> bytes:
        """The boot light's screen (splash.py): "Starting" in the middle and nothing that goes stale
        (no clock), packed for the panel, so it can be shown before Pillow or numpy load."""
        from .panel import to_rgb565
        img = Image.new("RGB", (W, H), (0, 0, 0))
        d = ImageDraw.Draw(img)
        self._title(d, LABELS[State.BOOT], DAY["fg"])
        for i, a in enumerate((0.9, 0.6, 0.35)):
            x = W / 2 + (i - 1) * 12
            d.ellipse([x - 3, INDICATOR_Y - 3, x + 3, INDICATOR_Y + 3], fill=tuple(int(c * a) for c in ACCENT[State.BOOT]))
        bgr = self.cfg.display.color_order.upper() == "BGR"
        return to_rgb565(self._orient(img), bgr).astype(">u2").tobytes()

    def clock_font(self, d, hhmm: str):
        """The largest light clock face that fits: as big as possible for "8:30", a little
        smaller for "12:30", and the same size all hour long so it doesn't jitter."""
        template = "00:00" if len(hhmm) > 4 else "0:00"     # "0" is about Inter's widest digit
        if template not in self._clock_sizes:
            size = 96
            while size > 30 and d.textlength(template, font=font(size, "display")) > W - 12:
                size -= 1
            self._clock_sizes[template] = size
        return font(self._clock_sizes[template], "display")

    def _idle(self, d, s, now, pal, dark):
        dx, dy = _ORBIT[now.minute % len(_ORBIT)] if self.cfg.display.burn_in_shift else (0, 0)
        hhmm, _ = format_clock(now, self.cfg.display.clock_24h)       # no AM/PM: it's a bedside clock
        d.text((W / 2 + dx, 40 + dy), now.strftime("%a, %b ") + str(now.day), font=font(15), fill=pal["dim"],
               anchor="mm")
        d.text((W / 2 + dx, 96 + dy), hhmm, font=self.clock_font(d, hhmm), fill=pal["fg"], anchor="mm")

        # everything else is one kind of item in the same place under the clock: (icon, text, colour, amount)
        items = []
        for lbl, due, total in s.timers[:2]:          # a bar that drains, like the LEDs
            left = due - time.time()
            items.append(("bar", f"{lbl.capitalize()} {countdown(left)}".strip(), mix(TIMER, pal["fg"], dark),
                          max(0.0, left / total) if total > 0 else 0.0))
        if s.task_state in TASK:
            icon, text, col = TASK[s.task_state]
            if s.task_state == "running" and s.task_label:    # what the agent is doing right now
                text = self._fit(d, s.task_label, W - 16 - self._ICON_W[icon])
            items.append((icon, text, mix(col, pal["fg"], dark), s.task_progress))
        if s.music:                                   # Spotify's now playing, cut to fit one row
            title = re.sub(r"\s*([-–(\[]).*\b(remaster(ed)?|version|edit|mono|stereo|live|feat|with)\b.*$", "",
                           s.music.get("title") or "", flags=re.I) or s.music.get("title") or ""
            text = " · ".join(x for x in (title, s.music.get("artist")) if x)
            room = W - 16 - self._ICON_W["note"]
            if d.textlength(text, font=font(13)) > room:
                text = title                          # the artist is the first thing to go
            items.append(("note", self._fit(d, text, room), pal["dim"], 0))
        if s.muted:
            items.append(("", "Mic off", mix(RED, pal["fg"], dark), 0))
        if not s.online:
            items.append(("", "Offline", mix(TIMER, pal["fg"], dark), 0))
        if s.next_alarm:
            items.append(("clock", s.next_alarm, pal["dim"], 0))
        rows = self._rows(d, items)
        for i, row in enumerate(rows):
            self._row(d, row, (146 + 18 * i if len(rows) > 1 else 152) + dy, dx)
        if s.quiet:
            self._bell_off(d, 14 + dx, 16 + dy, pal["dim"])

    @staticmethod
    def _fit(d, text: str, room: float) -> str:
        """Cut text to one row's width, with an ellipsis."""
        if d.textlength(text, font=font(13)) <= room:
            return text
        while text and d.textlength(text + "…", font=font(13)) > room:
            text = text[:-1]
        return text.rstrip(" ·") + "…"

    @staticmethod
    def _bell_off(d, x, y, col):
        """Quiet mode: a bell with a slash through it, centred on (x, y)."""
        d.pieslice([x - 5, y - 7, x + 5, y + 3], 180, 360, fill=col)          # dome
        d.rectangle([x - 5, y - 2, x + 5, y + 2], fill=col)                   # body
        d.polygon([(x - 7, y + 4), (x + 7, y + 4), (x + 5, y + 1), (x - 5, y + 1)], fill=col)   # flared rim
        d.ellipse([x - 2, y + 5, x + 2, y + 8], fill=col)                     # clapper
        d.line([(x - 8, y - 8), (x + 8, y + 8)], fill=(0, 0, 0), width=4)     # gap around the slash
        d.line([(x - 7, y - 7), (x + 7, y + 7)], fill=col, width=1)

    _ICON_W = {"bar": 28, "clock": 16, "dot": 12, "check": 15, "cross": 14, "note": 13, "": 0}
    _GAP = 14

    def _item_w(self, d, item) -> float:
        return self._ICON_W[item[0]] + d.textlength(item[1], font=font(13))

    def _rows(self, d, items, max_rows: int = 2) -> list[list]:
        """Pack the status items into up to two centred rows, most important first."""
        rows: list[list] = []
        for it in items:
            if rows and sum(self._item_w(d, x) + self._GAP for x in rows[-1]) + self._item_w(d, it) <= W - 16:
                rows[-1].append(it)
            elif len(rows) < max_rows:
                rows.append([it])
        return rows

    def _row(self, d, items, cy, dx):
        f = font(13)
        widths = [self._item_w(d, it) for it in items]
        cx = (W - sum(widths) - self._GAP * (len(items) - 1)) / 2 + dx
        for (icon, text, col, amount), w in zip(items, widths):
            self._icon(d, icon, cx, cy, col, amount)
            d.text((cx + self._ICON_W[icon], cy), text, font=f, fill=col, anchor="lm")
            cx += w + self._GAP

    @staticmethod
    def _icon(d, icon, x, cy, col, amount=0.0, pulse=1.0):
        if icon == "bar":           # timers drain it, jobs fill it
            d.rounded_rectangle([x, cy - 3, x + 22, cy + 3], radius=3, outline=col, width=1)
            if amount > 0:
                d.rounded_rectangle([x, cy - 3, x + max(6, 22 * min(1.0, amount)), cy + 3], radius=3, fill=col)
        elif icon == "dot":
            c = tuple(int(v * pulse) for v in col)
            d.ellipse([x + 1, cy - 3.5, x + 8, cy + 3.5], fill=c)
        elif icon == "note":        # a quaver
            d.ellipse([x, cy + 1, x + 5, cy + 5], fill=col)
            d.line([x + 5, cy + 3, x + 5, cy - 5], fill=col, width=1)
            d.line([x + 5, cy - 5, x + 8, cy - 2], fill=col, width=1)
        elif icon == "clock":
            d.ellipse([x, cy - 5, x + 10, cy + 5], outline=col, width=1)
            d.line([x + 5, cy, x + 5, cy - 3], fill=col)
            d.line([x + 5, cy, x + 7, cy + 1], fill=col)
        elif icon == "check":
            d.line([(x, cy), (x + 3, cy + 3), (x + 9, cy - 4)], fill=col, width=2)
        elif icon == "cross":
            d.line([(x, cy - 4), (x + 8, cy + 4)], fill=col, width=2)
            d.line([(x, cy + 4), (x + 8, cy - 4)], fill=col, width=2)

    def _header(self, d, s, now, pal, dark):
        hhmm, _ = format_clock(now, self.cfg.display.clock_24h)
        d.text((W / 2, 15), hhmm, font=font(13), fill=pal["dim"], anchor="mm")
        if s.quiet:
            self._bell_off(d, 14, 15, pal["dim"])
        if s.agent_mode:
            f = font(11, "medium")
            tw = d.textlength(s.agent_mode, font=f)
            col = mix(GOLD, pal["fg"], dark)
            d.rounded_rectangle([W - tw - 20, 7, W - 8, 23], radius=8, outline=col)
            d.text((W - 14, 15), s.agent_mode, font=f, fill=col, anchor="rm")
        elif s.muted:
            d.text((W - 10, 15), "Mic off", font=font(11), fill=mix(RED, pal["fg"], dark), anchor="rm")
        elif s.timers and s.state != State.ALARM:
            d.text((W - 10, 15), countdown(s.timers[0][1] - time.time()), font=font(12),
                   fill=mix(TIMER, pal["fg"], dark), anchor="rm")

    def _title(self, d, text, col, y=CENTER_Y):
        size = 20
        while size > 13 and d.textlength(text, font=font(size, "medium")) > W - 20:
            size -= 1
        d.text((W / 2, y), text, font=font(size, "medium"), fill=col, anchor="mm")

    def _centered_text(self, d, text, col, y, max_lines=3, size=13):
        f = font(size)
        for i, line in enumerate(wrap(d, text, f, W - 28)[:max_lines]):
            d.text((W / 2, y + i * 16), line, font=f, fill=col, anchor="mm")

    def _active(self, d, s, now, pal, dark):
        accent = GOLD if (s.agent_mode and s.state in (State.THINKING, State.SPEAKING)) else ACCENT.get(s.state, DAY["fg"])
        accent = mix(accent, pal["fg"], dark)
        self._header(d, s, now, pal, dark)
        t = time.monotonic()
        label = s.detail if s.state == State.WORKING and s.detail else LABELS.get(s.state, "")

        if s.state == State.SPEAKING:          # closed captions: the sentence being heard, nothing else
            self._captions(d, s, t, pal)
            return

        # everything else sits in the middle of the screen, your words (if any) quietly underneath
        if s.state == State.ERROR:
            self._title(d, "Something went wrong", pal["fg"])
            self._icon(d, "dot", W / 2 - 4.5, INDICATOR_Y, mix(RED, pal["fg"], dark))
            if s.detail:
                self._centered_text(d, s.detail, pal["dim"], INDICATOR_Y + 24)
            return
        self._title(d, label, pal["fg"])
        self._indicator(d, s, t, accent, y=INDICATOR_Y)
        if s.user_text and s.state != State.LISTENING:
            self._centered_text(d, f"“{s.user_text}”", pal["dim"], INDICATOR_Y + 26)

    CAPTION_LINES = 5
    CAPTION_CPS = 14.0          # about how many characters a second the voice speaks

    def _captions(self, d, s, t, pal):
        text = s.caption
        if not text and s.reply_text:          # no caption yet: the last sentence written so far
            text = re.split(r"(?<=[.!?])\s+", s.reply_text.strip())[-1]
        if not text:
            return
        f = font(16)
        lines = wrap(d, text, f, W - 24)
        if len(lines) > self.CAPTION_LINES:    # a long sentence scrolls at about the pace it's spoken
            said = (t - s.caption_at) * self.CAPTION_CPS if s.caption_at else 0
            at = int(said / max(1, len(text)) * len(lines))
            first = max(0, min(len(lines) - self.CAPTION_LINES, at - 1))
            lines = lines[first:first + self.CAPTION_LINES]
        top = 96 - (len(lines) - 1) * 10       # the block centred a little below the middle
        for i, line in enumerate(lines):
            d.text((W / 2, top + i * 20), line, font=f, fill=pal["fg"], anchor="mm")

    def _indicator(self, d, s, t, accent, y):
        if s.state in (State.LISTENING, State.SPEAKING):
            lvl = self.mic_level() if s.state == State.LISTENING else self.speaker_level()
            shape = np.array([0.45, 0.7, 0.9, 1.0, 0.9, 0.7, 0.45])
            wobble = 0.75 + 0.25 * np.sin(t * 9 + np.arange(7) * 1.3)
            target = 0.12 + 0.88 * lvl * shape * wobble
            self._bars += (target - self._bars) * 0.5
            for i, v in enumerate(self._bars):
                h = 3 + v * 14
                x = W / 2 + (i - 3) * 8
                d.rounded_rectangle([x - 1.5, y - h / 2, x + 1.5, y + h / 2], radius=1.5, fill=accent)
        else:          # transcribing, thinking, working, starting, shutting down: three soft dots
            for i in range(3):
                ph = (t * 2.2 - i * 0.22) % 1.0
                a = 0.25 + 0.75 * max(0.0, math.sin(ph * math.pi))
                x = W / 2 + (i - 1) * 12
                d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=tuple(int(c * a) for c in accent))

    def _setup(self, img, d, setup: dict) -> None:
        """Wi-Fi setup: a QR code that joins the setup hotspot, plus the same details as text."""
        amber = (255, 160, 70)
        if "connecting" in setup:
            d.text((W / 2, 72), "Joining", font=font(18, "medium"), fill=DAY["fg"], anchor="mm")
            d.text((W / 2, 98), setup["connecting"][:22], font=font(15), fill=DAY["dim"], anchor="mm")
            return
        err = setup.get("error")      # the full message is on the phone's setup page
        d.text((W / 2, 10), (setup.get("brief") or "Couldn't join: try again") if err else "Wi-Fi setup: scan me",
               font=font(12, "medium"),
               fill=RED if err else amber, anchor="mm")
        try:
            import segno
            esc = lambda v: "".join("\\" + c if c in '\\;,:"' else c for c in v)   # the Wi-Fi QR format's escaping
            qr = segno.make(f"WIFI:T:WPA;S:{esc(setup['ssid'])};P:{esc(setup['password'])};;", error="l",
                            boost_error=False)
            m = [[1] * (len(qr.matrix) + 4)] * 2
            m = m + [[1, 1] + [0 if v else 1 for v in row] + [1, 1] for row in qr.matrix] + m   # 1 = white
            scale = 100 // len(m)
            size = len(m) * scale
            x0, y0 = (W - size) // 2, 20
            for y, row in enumerate(m):
                for x, white in enumerate(row):
                    if white:
                        d.rectangle([x0 + x * scale, y0 + y * scale, x0 + (x + 1) * scale - 1,
                                     y0 + (y + 1) * scale - 1], fill=(255, 255, 255))
            y = y0 + size + 4
        except ImportError:          # no QR library: the text below is enough
            y = 40
        f = font(12)
        for line in (f"Wi-Fi  {setup['ssid']}", f"password  {setup['password']}", f"then open {setup['url'][7:]}"):
            d.text((W / 2, y + 6), line, font=f, fill=DAY["fg"], anchor="mm")
            y += 15

    def _alarm(self, d, s, now, pal, dark):
        """A timer or alarm going off: the same layout as the other screens, with the timer's bar
        (now empty) pulsing where the dots would be."""
        self._header(d, s, now, pal, dark)
        col = mix(ACCENT[State.ALARM], pal["fg"], dark)
        pulse = 0.35 + 0.65 * math.sin(time.monotonic() * math.pi) ** 2
        self._title(d, s.detail or "Alarm", pal["fg"])
        x, y = W / 2 - 22, INDICATOR_Y
        d.rounded_rectangle([x, y - 4, x + 44, y + 4], radius=4, outline=col, width=1)
        d.rounded_rectangle([x, y - 4, x + 44, y + 4], radius=4, fill=tuple(int(c * pulse) for c in col))
        if "timer" in (s.detail or "").lower():
            d.text((W / 2, INDICATOR_Y + 26), "Time's up", font=font(13), fill=pal["dim"], anchor="mm")
        d.text((W / 2, 160), "Press a button or say Franklin", font=font(11), fill=pal["faint"], anchor="mm")

    def _power(self, d, s, pal):
        self._title(d, "Restarting" if s.power == "restart" else "Shutting down", pal["fg"])
        self._indicator(d, s, time.monotonic(), pal["dim"], y=INDICATOR_Y)
        if s.power != "restart":
            self._centered_text(d, "Unplug me once the screen goes dark", pal["dim"], INDICATOR_Y + 26, size=12)
