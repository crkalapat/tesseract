"""Night mode ("good night" -> dim and warm, easing back from 6:30 to 7:30) and the screen brightness."""

import datetime as dt
import time

import pytest

from tesseract import config
from tesseract.alarms import Alarms
from tesseract.commands import GOOD_MORNING, GOOD_NIGHT, RESTARTING, SHUTTING_DOWN, LocalCommands
from tesseract.display import Display, panel_levels
from tesseract.state import State, Status
from tesseract.util import blend, darkness, next_at


def at(h, m, day=30):
    return dt.datetime(2026, 10, day, h, m)


@pytest.fixture
def cfg():
    c = config.load()
    c.night.start, c.night.sunrise_start, c.night.sunrise_end = "02:00", "06:30", "07:30"
    return c


def test_good_night_then_sunrise(cfg):
    said = at(23, 15)
    until = next_at(cfg.night.sunrise_end, said).timestamp()            # tomorrow 7:30
    assert darkness(cfg, at(23, 14)) == 0                              # before: daytime screen
    assert darkness(cfg, at(23, 20), night_until=until) == 1
    assert darkness(cfg, at(3, 0, 31), night_until=until) == 1          # all night
    assert darkness(cfg, at(6, 30, 31), night_until=until) == 1         # the ramp starts at 6:30...
    assert darkness(cfg, at(7, 0, 31), night_until=until) == pytest.approx(0.5)
    assert darkness(cfg, at(7, 29, 31), night_until=until) < 0.02       # ...and is done at 7:30
    assert darkness(cfg, at(7, 30, 31), night_until=until) == 0


def test_forgot_to_say_good_night(cfg):
    assert darkness(cfg, at(1, 59)) == 0
    assert darkness(cfg, at(2, 0)) == 1                                 # [night] start
    assert darkness(cfg, at(7, 0)) == pytest.approx(0.5)                # same sunrise
    cfg.night.start = ""
    assert darkness(cfg, at(3, 0)) == 0                                 # turned off


def test_good_morning_ends_it(cfg):
    until = next_at(cfg.night.sunrise_end, at(23, 0)).timestamp()
    morning = next_at(cfg.night.start, at(6, 0, 31)).timestamp()
    assert darkness(cfg, at(6, 5, 31), night_until=until, day_until=morning) == 0


def test_screen_brightness():
    assert blend(1.0, 0.1, 0) == 1.0 and blend(1.0, 0.1, 1) == pytest.approx(0.1)
    assert blend(1.0, 0.1, 0.5) == pytest.approx(0.316, abs=0.01)       # halfway *to the eye* (log scale)
    assert panel_levels(1.0) == (15, 1.0)
    step, scale = panel_levels(0.1)
    assert step == 1 and (step + 1) / 16 * scale == pytest.approx(0.1)  # coarse current + fine pixel scale
    cfg = config.load()
    disp = Display(cfg, Status(), sim=True)
    s = Status()
    s.set(screen=6)
    assert disp.fraction(s, at(12, 0))[0] == pytest.approx(0.6)
    s.set(screen=0)
    assert disp.fraction(s, at(12, 0))[0] == 0                          # off


class Hooks:
    def __init__(self):
        self.level, self.calls = 10, []
    def set_volume(self, v): pass
    def volume(self): return 5
    def set_screen(self, v): self.level = max(0, min(10, v))
    def screen(self): return self.level
    def good_night(self): self.calls.append("night")
    def good_morning(self): self.calls.append("morning")
    def power_off(self, restart=False): self.calls.append("restart" if restart else "off")


@pytest.mark.parametrize("said,reply,call", [
    ("good night franklin", GOOD_NIGHT, "night"), ("goodnight", GOOD_NIGHT, "night"),
    ("im going to bed", GOOD_NIGHT, "night"), ("good morning", GOOD_MORNING, "morning")])
def test_night_commands(tmp_path, said, reply, call):
    hooks = Hooks()
    r = LocalCommands(config.load(), Alarms(tmp_path / "a.json", on_fire=print), hooks).handle(said)
    assert r.text == reply and hooks.calls == []
    r.then()                                     # runs after the reply has been spoken
    assert hooks.calls == [call]


@pytest.mark.parametrize("said,before,after", [
    ("dim the screen", 10, 7), ("make the display brighter", 4, 7), ("set the screen brightness to 3", 10, 3),
    ("screen brightness 50 percent", 10, 5), ("turn off the screen", 6, 0), ("turn on the display", 0, 10),
    ("the screen is too bright", 2, 1)])
def test_screen_commands(tmp_path, said, before, after):
    hooks = Hooks()
    hooks.level = before
    r = LocalCommands(config.load(), Alarms(tmp_path / "a.json", on_fire=print), hooks).handle(said)
    assert r is not None and r.text == "" and hooks.level == after


def test_every_screen_renders_day_and_night():
    cfg = config.load()
    disp = Display(cfg, Status(), sim=True)
    for state in State:
        for dark in (0.0, 0.5, 1.0):
            s = Status()
            s.set(state=state, timers=[("pasta", time.time() + 60, 120)], next_alarm="7:00 AM", user_text="hi",
                  reply_text="hello", detail="Pasta timer")
            img = disp.render(s, at(9, 41), dark)
            assert img.size == (176, 176) and img.getbbox() is not None


@pytest.mark.parametrize("said,reply,call", [
    ("shut down", SHUTTING_DOWN, "off"), ("shut yourself down", SHUTTING_DOWN, "off"),
    ("power off", SHUTTING_DOWN, "off"), ("turn yourself off", SHUTTING_DOWN, "off"),
    ("restart", RESTARTING, "restart"), ("reboot yourself", RESTARTING, "restart")])
def test_power_commands(tmp_path, said, reply, call):
    hooks = Hooks()
    r = LocalCommands(config.load(), Alarms(tmp_path / "a.json", on_fire=print), hooks).handle(said)
    assert r.text == reply
    r.then()
    assert hooks.calls == [call]


@pytest.mark.parametrize("said", ["turn off the screen", "shut up", "restart the timer", "turn off the lights"])
def test_not_power_commands(tmp_path, said):
    hooks = Hooks()
    r = LocalCommands(config.load(), Alarms(tmp_path / "a.json", on_fire=print), hooks).handle(said)
    assert r is None or r.text not in (SHUTTING_DOWN, RESTARTING)


def test_status_items_share_one_place():
    """Timers, the job and the alarm are the same kind of item, in the same rows under the clock."""
    cfg = config.load()
    cfg.display.rotation, cfg.display.mirror = 0, ""
    disp = Display(cfg, Status(), sim=True)
    s = Status()
    s.set(state=State.IDLE, timers=[("pasta", time.time() + 272, 600)], task_state="waiting", next_alarm="7:00 AM")
    img = disp.render(s, at(20, 30))
    assert img.crop((0, 136, 176, 176)).getbbox() is not None
    s.set(power="off")
    assert disp.render(s, at(20, 30)).getbbox() is not None


@pytest.mark.parametrize("said,on", [("switch to local mode", True), ("use the local model", True),
                                     ("use the cloud", False), ("switch back to the cloud", False)])
def test_local_mode_commands(tmp_path, said, on):
    hooks = Hooks()
    hooks.set_local = lambda v: hooks.calls.append(v) or "ok"
    r = LocalCommands(config.load(), Alarms(tmp_path / "a.json", on_fire=print), hooks).handle(said)
    assert r.text == "ok" and hooks.calls == [on]
