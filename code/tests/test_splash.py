"""The boot light: up within seconds of power-on, so it must not pull in the heavy stuff."""

import subprocess
import sys

import numpy as np

from tesseract import config, splash
from tesseract.display import Display
from tesseract.state import Status


def test_boot_light_stays_light():
    code = ("import sys, tesseract.splash, tesseract.panel; "
            "heavy = [m for m in ('numpy', 'PIL', 'httpx', 'onnxruntime', 'sherpa_onnx', 'gpiozero') if m in sys.modules]; "
            "print(heavy)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]", out


def test_same_rainbow_as_the_speaker():
    """The speaker carries the boot light's rainbow on: same colours at the same moment."""
    from tesseract.leds import Leds
    cfg = config.load()
    leds = Leds(cfg, Status(), sim=True, continuing=True)
    now = 1234.5
    import time
    real = time.monotonic
    time.monotonic = lambda: now
    try:
        frame = leds._scene(Status(), 0.0, 99.0)
    finally:
        time.monotonic = real
    for i, x in enumerate(leds.x):
        assert np.allclose(frame[i], splash.rainbow(float(x), now, 99.0), atol=1e-6)
    assert splash.to_led(0.0, 0.35) == 0 and splash.to_led(0.06, 0.04) == 1 and splash.to_led(1.0, 1.0) == 255


def test_boot_screen_frame():
    cfg = config.load()
    frame = Display(cfg, Status(), sim=True).splash_frame()
    assert len(frame) == 176 * 176 * 2 and any(frame)        # a whole RGB565 frame with "Starting" on it
