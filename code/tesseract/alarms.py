"""Timers and alarms, persisted so they survive a reboot."""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .util import format_clock, parse_hhmm

log = logging.getLogger(__name__)

REPEATS = ("once", "daily", "weekdays", "weekends")


@dataclass
class Item:
    id: str
    kind: str          # "timer" | "alarm"
    label: str
    due: float         # unix time
    repeat: str = "once"
    hhmm: str = ""     # alarms only
    total: float = 0.0  # timers only: the length it was set for (for the countdown bar)

    def describe(self, h24: bool = False) -> str:
        when = dt.datetime.fromtimestamp(self.due)
        if self.kind == "timer":
            left = max(0, int(self.due - time.time()))
            m, s = divmod(left, 60)
            return f"{self.label or 'timer'}: {m} min {s} s left"
        hhmm, ampm = format_clock(when, h24)
        rep = "" if self.repeat == "once" else f", {self.repeat}"
        return f"{self.label or 'alarm'} at {hhmm} {ampm} {when:%a}{rep}".replace("  ", " ")


def next_occurrence(hhmm: str, repeat: str, after: dt.datetime | None = None) -> dt.datetime:
    after = after or dt.datetime.now()
    t = parse_hhmm(hhmm)
    cand = after.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if cand <= after:
        cand += dt.timedelta(days=1)
    for _ in range(8):
        wd = cand.weekday()
        if repeat in ("once", "daily") or (repeat == "weekdays" and wd < 5) or (repeat == "weekends" and wd >= 5):
            return cand
        cand += dt.timedelta(days=1)
    return cand


class Alarms:
    def __init__(self, path: Path, on_fire, on_change=lambda: None):
        self.path = path
        self.on_fire = on_fire
        self.on_change = on_change
        self.items: list[Item] = []
        self._lock = threading.RLock()   # on_change() calls back into list()
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
            self.items = [Item(**r) for r in raw]
        except FileNotFoundError:
            return
        except Exception:
            log.exception("alarms: couldn't read %s", self.path)
            return
        now = time.time()
        for it in list(self.items):  # handle things that came due while we were off
            if it.due < now - 600:
                if it.repeat == "once":
                    self.items.remove(it)
                else:
                    it.due = next_occurrence(it.hhmm, it.repeat).timestamp()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(i) for i in self.items], indent=1))
        tmp.replace(self.path)
        self.on_change()

    def start(self) -> None:
        threading.Thread(target=self._run, name="alarms", daemon=True).start()

    def _run(self) -> None:
        while True:
            now = time.time()
            fired = []
            with self._lock:
                for it in list(self.items):
                    if it.due <= now:
                        fired.append(Item(**asdict(it)))
                        if it.repeat == "once":
                            self.items.remove(it)
                        else:
                            it.due = next_occurrence(it.hhmm, it.repeat,
                                                     dt.datetime.fromtimestamp(now + 1)).timestamp()
                if fired:
                    self._save()
            for it in fired:
                log.info("alarms: %s fired (%s)", it.kind, it.label)
                try:
                    self.on_fire(it)
                except Exception:
                    log.exception("alarms: on_fire failed")
            time.sleep(0.5)

    def add_timer(self, seconds: float, label: str = "") -> Item:
        it = Item(uuid.uuid4().hex[:6], "timer", label.strip(), time.time() + float(seconds), total=float(seconds))
        with self._lock:
            self.items.append(it)
            self._save()
        return it

    def add_alarm(self, hhmm: str, label: str = "", repeat: str = "once") -> Item:
        repeat = repeat if repeat in REPEATS else "once"
        due = next_occurrence(hhmm, repeat).timestamp()
        it = Item(uuid.uuid4().hex[:6], "alarm", label.strip(), due, repeat, hhmm)
        with self._lock:
            self.items.append(it)
            self._save()
        return it

    def add_at(self, when: dt.datetime, label: str = "") -> Item:
        """A one-off alarm at a specific date and time (calendar reminders)."""
        it = Item(uuid.uuid4().hex[:6], "alarm", label.strip(), when.timestamp(), "once", f"{when:%H:%M}")
        with self._lock:
            self.items.append(it)
            self._save()
        return it

    def timers(self) -> list[Item]:
        return [i for i in self.list() if i.kind == "timer"]

    def cancel(self, which: str = "") -> list[Item]:
        """which: an id, a label, "timers", "alarms", or "all"/"" (everything)."""
        w = which.strip().lower()
        with self._lock:
            def match(i: Item) -> bool:
                return (w in ("", "all") or i.id == w or (i.label and i.label.lower() == w)
                        or (w in ("timer", "timers") and i.kind == "timer")
                        or (w in ("alarm", "alarms") and i.kind == "alarm"))
            gone = [i for i in self.items if match(i)]
            self.items = [i for i in self.items if i not in gone]
            if gone:
                self._save()
        return gone

    def list(self) -> list[Item]:
        with self._lock:
            return sorted(self.items, key=lambda i: i.due)

    def next_alarm_text(self, h24: bool = False) -> str:
        alarms = [i for i in self.list() if i.kind == "alarm"]
        if not alarms:
            return ""
        nxt = dt.datetime.fromtimestamp(alarms[0].due)
        hhmm, ampm = format_clock(nxt, h24)
        soon = nxt - dt.datetime.now() < dt.timedelta(hours=24)
        return f"{hhmm} {ampm}".strip() if soon else f"{nxt:%a} {hhmm} {ampm}".strip()
