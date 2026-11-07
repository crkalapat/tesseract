"""The boot light: a sign of life a few seconds after Tesseract is plugged in.

The speaker itself takes 20-30 s to start (Python loading numpy, the speech models, ...), and
until then nothing happens, which is awkward in a demo. This is a tiny service of its own
(systemd/tesseract-splash.service) that systemd starts right after the kernel, with no heavy
imports:

  * the LED strip does the same slow rainbow as the speaker's "Starting" state, on the same
    clock, so when the speaker takes over the animation just carries on
  * the screen shows the "Starting" frame the speaker saved the last time it ran
    (data/splash.rgb565, so it follows the rotation and colour settings)

When the speaker is ready to drive the LEDs and the screen, it stops this (stop() below).
"""

from __future__ import annotations

import json
import logging
import math
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

FPS = 30
GIVE_UP_S = 180          # the speaker never took over (not enabled? crashing?): go dark
UNIT = "tesseract-splash.service"


def rainbow(x: float, now: float, age: float) -> tuple[float, float, float]:
    """The "Starting" rainbow at strip position x (0..1), the same as leds.py's BOOT scene."""
    k = 0.7 * min(1.0, age * 2)
    return tuple((0.5 + 0.5 * math.cos(2 * math.pi * ((x + now * 0.5 + c / 3) % 1.0))) * k for c in range(3))


def to_led(v: float, cap: float) -> int:
    """0..1 -> 0..255 like leds.to_pixels: gamma, then the brightness cap, dim colours kept visible."""
    out = int(v ** 2.2 * cap * 255 + 0.5)
    return 1 if out == 0 and v > 0.05 else out


def open_strip(cfg):
    """The WS2812B strip through rpi_ws281x (also used by leds.py)."""
    from rpi_ws281x import PixelStrip, ws
    pin = cfg.pins.led_data
    if pin not in (12, 13, 18, 19):
        raise RuntimeError(f"GPIO{pin} can't drive WS2812s; use a PWM pin (12 or 13)")
    types = {"GRB": ws.WS2811_STRIP_GRB, "RGB": ws.WS2811_STRIP_RGB, "BRG": ws.WS2811_STRIP_BRG,
             "RBG": ws.WS2811_STRIP_RBG, "GBR": ws.WS2811_STRIP_GBR, "BGR": ws.WS2811_STRIP_BGR}
    strip = PixelStrip(cfg.leds.count, pin, 800_000, cfg.leds.dma, False, 255, 1 if pin in (13, 19) else 0,
                       types.get(cfg.leds.strip_type.upper(), ws.WS2811_STRIP_GRB))
    strip.begin()
    return strip


def screen_level(cfg) -> int:
    """The screen brightness (0-10) the speaker last used."""
    try:
        return int(json.loads((cfg.path(cfg.system.state_dir) / "settings.json").read_text()).get("screen", 10))
    except (OSError, ValueError):
        return cfg.display.brightness


def show_screen(cfg) -> None:
    """Put the saved "Starting" frame on the screen (waits for the SPI device to appear)."""
    frame = cfg.path(cfg.system.state_dir) / "splash.rgb565"
    level = screen_level(cfg)
    if not frame.exists() or level <= 0:
        return
    dev = Path(f"/dev/spidev{cfg.pins.display_spi_bus}.{cfg.pins.display_spi_device}")
    for _ in range(100):              # its kernel module loads a moment into boot
        if dev.exists():
            break
        time.sleep(0.05)
    import lgpio                      # much lighter than gpiozero, which the speaker uses

    from .panel import SSD1333
    chip = lgpio.gpiochip_open(0)

    class Pin:
        def __init__(self, gpio, initial):
            self.gpio = gpio
            lgpio.gpio_claim_output(chip, gpio, int(initial))

        def on(self):
            lgpio.gpio_write(chip, self.gpio, 1)

        def off(self):
            lgpio.gpio_write(chip, self.gpio, 0)

    panel = SSD1333(cfg, pin=Pin)
    panel.brightness(max(0, min(15, math.ceil(level / 10 * 16) - 1)))
    panel.show_raw(frame.read_bytes())


def run() -> int:
    t_start = time.monotonic()
    cfg = config.load()
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())     # the speaker is taking over
    threading.Thread(target=_screen_safely, args=(cfg,), daemon=True).start()
    strip = None
    if cfg.leds.enabled:
        try:
            strip = open_strip(cfg)
        except Exception as e:
            log.warning("splash: no LEDs (%s)", e)
    n = cfg.leds.count
    xs = [i / (n - 1) if n > 1 else 0.5 for i in range(n)]
    if cfg.leds.reverse:
        xs.reverse()
    cap = cfg.leds.brightness
    while not done.is_set() and time.monotonic() - t_start < GIVE_UP_S:
        now = time.monotonic()
        if strip is not None:
            for i, x in enumerate(xs):
                r, g, b = (to_led(v, cap) for v in rainbow(x, now, now - t_start))
                strip.setPixelColor(i, (r << 16) | (g << 8) | b)
            strip.show()
        done.wait(1 / FPS)
    if strip is not None and not done.is_set():                # gave up: don't leave a rainbow running
        for i in range(n):
            strip.setPixelColor(i, 0)
        strip.show()
    return 0                                                   # stopped: the LEDs keep their last colours


def _screen_safely(cfg) -> None:
    try:
        show_screen(cfg)
    except Exception as e:
        log.warning("splash: no screen (%s)", e)


def stop() -> None:
    """Called by the speaker just before it opens the LEDs and the screen. Waits until the boot light
    has let go of them; does nothing if it isn't running (or this isn't the Pi)."""
    if not Path("/run/systemd/system").exists():
        return
    try:
        subprocess.run(["systemctl", "stop", UNIT], timeout=5, check=False, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")
    raise SystemExit(run())
