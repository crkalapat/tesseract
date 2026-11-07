"""The four buttons. Pins come from [pins] in config.toml.

action    tap  = talk (or stop talking / dismiss alarm)
          hold = cancel everything and forget the conversation
          hold 5 s = shut down (unplug once the screen goes dark)
vol up/down    = step volume; hold to keep stepping
               both held for 3 s = Wi-Fi setup (the setup hotspot; again to cancel)
mute           = toggle the microphone (LEDs glow red while muted)
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)


class Buttons:
    def __init__(self, cfg, on_action, on_action_hold, on_volume, on_mute, sim=False, on_setup=lambda: None,
                 on_power_off=lambda: None):
        self._held = self._off = False
        self._buttons = []
        if sim:          # the simulator reads keys from the terminal instead (app.py)
            return
        try:
            from gpiozero import Button
        except ImportError as e:
            log.warning("buttons: disabled (%s)", e)
            return
        p = cfg.pins

        def make(pin, **kw):
            try:
                b = Button(pin, pull_up=True, bounce_time=0.03, **kw)
                self._buttons.append(b)
                return b
            except Exception as e:
                log.error("buttons: GPIO%s unavailable (%s)", pin, e)
                return None

        if a := make(p.button_action, hold_time=1.0, hold_repeat=True):     # "held" fires every second
            def held():
                if not self._held:
                    self._held = True
                    on_action_hold()
                elif not self._off and (a.held_time or 0) >= 5:
                    self._off = True
                    on_power_off()

            def released():
                if not self._held:
                    on_action()
                self._held = self._off = False
            a.when_held = held
            a.when_released = released

        vol = []
        for pin, step in ((p.button_vol_up, +1), (p.button_vol_down, -1)):
            if b := make(pin, hold_time=0.5, hold_repeat=True):
                b.when_pressed = (lambda s=step: on_volume(s))
                b.when_held = (lambda s=step: on_volume(s))
                vol.append(b)
        if len(vol) == 2:
            threading.Thread(target=self._combo, args=(vol, on_setup), name="buttons", daemon=True).start()

        if m := make(p.button_mute):
            m.when_pressed = on_mute

    @staticmethod
    def _combo(vol, on_setup, hold_s: float = 3.0) -> None:
        """Both volume buttons held together for hold_s -> on_setup(), once per hold."""
        since, fired = None, False
        while True:
            time.sleep(0.1)
            try:
                both = all(b.is_pressed for b in vol)
            except Exception:      # the buttons were closed (shutting down)
                return
            if both:
                since = since or time.monotonic()
                if not fired and time.monotonic() - since >= hold_s:
                    fired = True
                    on_setup()
            else:
                since, fired = None, False
