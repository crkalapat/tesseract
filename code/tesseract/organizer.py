"""The built-in agent's to-do list, calendar and notes, kept in one JSON file on
the device (data/organizer.json) so they survive reboots and need no account."""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import uuid
from pathlib import Path

log = logging.getLogger(__name__)


def _id() -> str:
    return uuid.uuid4().hex[:6]


def parse_when(s: str) -> dt.datetime:
    """'2026-10-24T18:00', '2026-10-24 18:00' or '2026-10-24' (all day) -> datetime."""
    s = s.strip().replace(" ", "T")
    return dt.datetime.fromisoformat(s[:16] if "T" in s else s)


class Organizer:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.data = {"todos": [], "events": [], "notes": []}
        try:
            self.data.update(json.loads(path.read_text()))
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("organizer: couldn't read %s", path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    def _find(self, kind: str, which: str) -> dict | None:
        """By id, else the first item whose text/title contains `which`."""
        w = which.strip().lower()
        items = self.data[kind]
        for it in items:
            if it["id"] == w:
                return it
        for it in items:
            if w and w in (it.get("text") or it.get("title") or "").lower():
                return it
        return None

    # ── to-do ───────────────────────────────────────────────────────────────
    def todo_add(self, text: str, due: str = "") -> dict:
        it = {"id": _id(), "text": text.strip(), "due": due.strip(), "done": False,
              "created": dt.datetime.now().isoformat(timespec="minutes")}
        with self._lock:
            self.data["todos"].append(it)
            self._save()
        return it

    def todos(self, include_done: bool = False) -> list[dict]:
        with self._lock:
            items = [t for t in self.data["todos"] if include_done or not t["done"]]
        return sorted(items, key=lambda t: (t["due"] or "9999", t["created"]))

    def todo_update(self, which: str, action: str) -> dict | None:
        with self._lock:
            it = self._find("todos", which)
            if it is None:
                return None
            if action == "remove":
                self.data["todos"].remove(it)
            else:
                it["done"] = action != "reopen"
            self._save()
        return it

    # ── calendar ────────────────────────────────────────────────────────────
    def event_add(self, title: str, start: str, end: str = "", location: str = "", details: str = "") -> dict:
        s = parse_when(start)
        it = {"id": _id(), "title": title.strip(), "start": s.isoformat(timespec="minutes"),
              "end": parse_when(end).isoformat(timespec="minutes") if end else "",
              "all_day": "T" not in start.strip().replace(" ", "T"),
              "location": location.strip(), "details": details.strip()}
        with self._lock:
            self.data["events"].append(it)
            self._save()
        return it

    def events_between(self, start: dt.datetime, end: dt.datetime) -> list[dict]:
        with self._lock:
            items = [e for e in self.data["events"] if start <= dt.datetime.fromisoformat(e["start"]) < end]
        return sorted(items, key=lambda e: e["start"])

    def event_remove(self, which: str) -> dict | None:
        with self._lock:
            it = self._find("events", which)
            if it is not None:
                self.data["events"].remove(it)
                self._save()
        return it

    # ── notes ───────────────────────────────────────────────────────────────
    def note_add(self, text: str) -> dict:
        it = {"id": _id(), "text": text.strip(), "created": dt.datetime.now().isoformat(timespec="minutes")}
        with self._lock:
            self.data["notes"].append(it)
            self._save()
        return it

    def notes(self, query: str = "") -> list[dict]:
        q = query.strip().lower()
        with self._lock:
            return [n for n in self.data["notes"] if not q or any(w in n["text"].lower() for w in q.split())]
