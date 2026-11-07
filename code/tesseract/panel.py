"""The SSD1333 OLED driver (NHD-1.91-176176UBC3: 176x176 RGB565 over SPI).

Kept apart from display.py, and free of numpy and Pillow until a frame is actually drawn, so
the boot light (splash.py) can put a screen up a few seconds after power-on.
"""

from __future__ import annotations

import time

W = H = 176

# Init sequence from the Newhaven NHD-1.91-176176UBC3 datasheet (p.17).
GAMMA = [0x00, 0x02, 0x04, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x10, 0x11, 0x12,
         0x14, 0x15, 0x16, 0x18, 0x1a, 0x1b, 0x1d, 0x1f, 0x20, 0x22, 0x24, 0x26, 0x27, 0x29, 0x2b,
         0x2d, 0x2f, 0x31, 0x33, 0x35, 0x37, 0x39, 0x3b, 0x3d, 0x3f, 0x42, 0x44, 0x46, 0x48, 0x4b,
         0x4d, 0x50, 0x52, 0x55, 0x57, 0x5a, 0x5b, 0x5f, 0x62, 0x65, 0x67, 0x6a, 0x6d, 0x70, 0x73,
         0x76, 0x79, 0x7c]


class SSD1333:
    def __init__(self, cfg, pin=None):
        """pin(gpio, initial) -> an object with on()/off(); gpiozero's by default."""
        import spidev
        if pin is None:
            from gpiozero import DigitalOutputDevice
            pin = lambda gpio, initial: DigitalOutputDevice(gpio, initial_value=initial)

        p = cfg.pins
        self.spi = spidev.SpiDev()
        self.spi.open(p.display_spi_bus, p.display_spi_device)
        self.spi.max_speed_hz = cfg.display.spi_hz
        self.spi.mode = 0
        self.dc = pin(p.display_dc, False)
        self.rst = pin(p.display_reset, True)
        self.bgr = cfg.display.color_order.upper() == "BGR"
        self._last = None
        self.reset()

    def _cmd(self, c: int, *data: int) -> None:
        self.dc.off()
        self.spi.writebytes([c])
        if data:
            self.dc.on()
            self.spi.writebytes(list(data))

    def reset(self) -> None:
        self.rst.off()
        time.sleep(0.01)
        self.rst.on()
        time.sleep(0.05)
        c = self._cmd
        c(0xFD, 0x12)              # unlock
        c(0xAE)                    # display off
        c(0xA0, 0x64)              # remap: horizontal increment, 65k colour, COM split
        c(0xA1, 0x00)              # start line
        c(0xA2, 0x00)              # display offset
        c(0xA6)                    # normal display
        c(0xB1, 0x24)              # phase length
        c(0xB3, 0x80)              # display clock
        c(0xB6, 0x0F)              # second pre-charge period
        c(0xBB, 0x1F)              # pre-charge voltage 0.40*VCC
        c(0xBE, 0x05)              # VCOMH 0.82*VCC
        c(0xC1, 0x3C, 0x39, 0x43)  # contrast A, B, C
        c(0xC7, 0x0F)              # master current
        c(0xCA, 0xAF)              # 176 MUX
        c(0xB8, *GAMMA)            # gamma LUT
        self.clear()
        c(0xAF)                    # display on
        self._last = None

    def clear(self) -> None:
        self._window(0, 0, W - 1, H - 1)
        self.spi.writebytes2(bytes(W * H * 2))
        self._last = None

    def show_raw(self, rgb565: bytes) -> None:
        """A whole frame already packed as big-endian RGB565 (the boot light's "Starting" screen)."""
        self._window(0, 0, W - 1, H - 1)
        self.spi.writebytes2(rgb565)
        self._last = None

    def brightness(self, level: int) -> None:
        self._cmd(0xC7, max(0, min(15, int(level))))

    def power(self, on: bool) -> None:
        self._cmd(0xAF if on else 0xAE)

    def _window(self, x0, y0, x1, y1) -> None:
        self._cmd(0x15, x0, x1)
        self._cmd(0x75, y0, y1)
        self._cmd(0x5C)
        self.dc.on()

    def show(self, img) -> None:
        """Send a Pillow image, only the rectangle that changed since the last one."""
        import numpy as np
        px = to_rgb565(img, self.bgr)
        if self._last is not None:
            diff = px != self._last
            rows, cols = np.flatnonzero(diff.any(axis=1)), np.flatnonzero(diff.any(axis=0))
            if not len(rows):
                return
            y0, y1, x0, x1 = rows[0], rows[-1], cols[0], cols[-1]
        else:
            y0, y1, x0, x1 = 0, H - 1, 0, W - 1
        self._window(int(x0), int(y0), int(x1), int(y1))
        self.spi.writebytes2(px[y0:y1 + 1, x0:x1 + 1].astype(">u2").tobytes())
        self._last = px

    def close(self) -> None:
        try:
            self.power(False)
        finally:
            self.spi.close()


def to_rgb565(img, bgr: bool = False):
    """A Pillow RGB image -> a 176x176 uint16 array of RGB565 pixels."""
    import numpy as np
    a = np.asarray(img, dtype=np.uint16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    if bgr:
        r, b = b, r
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
