"""Reminder scheduling: turning human phrasing into a time, and firing on it."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .memory import MongoStore, Reminder, utcnow

_UNITS = {
    "s": "seconds", "sec": "seconds", "secs": "seconds",
    "second": "seconds", "seconds": "seconds",
    "m": "minutes", "min": "minutes", "mins": "minutes",
    "minute": "minutes", "minutes": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
    "d": "days", "day": "days", "days": "days",
    "w": "weeks", "week": "weeks", "weeks": "weeks",
}

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

_RECURRENCES = ("hourly", "daily", "weekdays", "weekly", "monthly")


class TimeParseError(ValueError):
    """The phrasing did not resolve to a time."""


def _clock(text: str) -> tuple[int, int] | None:
    """'9am', '09:00', '5:30pm', 'noon' -> (hour, minute)."""
    text = text.strip().lower()
    if text in {"noon", "midday"}:
        return 12, 0
    if text == "midnight":
        return 0, 0
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def parse_when(text: str, now: datetime | None = None) -> datetime:
    """Resolve a phrase to a local-time datetime.

    Understands ISO-8601, 'in 10 minutes', 'tomorrow at 9am', 'friday 17:00',
    and a bare clock time (which rolls to tomorrow if it already passed).
    """
    raw = (text or "").strip().lower()
    if not raw:
        raise TimeParseError("no time given")
    base = now or datetime.now().astimezone()
    if base.tzinfo is None:
        base = base.astimezone()

    try:
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=base.tzinfo)
    except ValueError:
        pass

    match = re.fullmatch(r"(?:in\s+)?(\d+(?:\.\d+)?)\s*([a-z]+)(?:\s+from\s+now)?", raw)
    if match and match.group(2) in _UNITS:
        return base + timedelta(**{_UNITS[match.group(2)]: float(match.group(1))})

    stripped = re.sub(r"\b(at|on|this|next)\b", " ", raw)
    stripped = re.sub(r"\s+", " ", stripped).strip()

    if stripped.startswith(("tomorrow", "tmr")):
        rest = stripped.split(" ", 1)[1] if " " in stripped else ""
        hour, minute = _clock(rest) or (9, 0)
        return (base + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )

    if stripped.startswith("today") or stripped.startswith("tonight"):
        rest = stripped.split(" ", 1)[1] if " " in stripped else ""
        default = (20, 0) if stripped.startswith("tonight") else (9, 0)
        hour, minute = _clock(rest) or default
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    head = stripped.split(" ", 1)[0]
    if head in _WEEKDAYS:
        rest = stripped.split(" ", 1)[1] if " " in stripped else ""
        hour, minute = _clock(rest) or (9, 0)
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        ahead = (_WEEKDAYS[head] - base.weekday()) % 7
        if ahead == 0 and target <= base:
            ahead = 7
        return target + timedelta(days=ahead)

    clock = _clock(stripped)
    if clock:
        candidate = base.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if candidate <= base:
            candidate += timedelta(days=1)
        return candidate

    raise TimeParseError(
        f"could not read {text!r} as a time - try an ISO timestamp like "
        "2026-09-18T09:00, 'in 30 minutes', or 'tomorrow at 9am'"
    )


def parse_recurrence(text: str | None) -> str | None:
    """Normalize a repeat spec, or raise if it is not one we support."""
    if not text:
        return None
    raw = text.strip().lower()
    if raw in {"none", "once", "never"}:
        return None
    if raw in _RECURRENCES:
        return raw
    match = re.fullmatch(r"every\s*:?\s*(\d+)\s*([a-z]+)", raw)
    if match and match.group(2) in _UNITS:
        return f"every:{match.group(1)}{match.group(2)}"
    raise TimeParseError(
        f"unsupported recurrence {text!r} - use one of {_RECURRENCES} or 'every 30 minutes'"
    )


def next_occurrence(recurrence: str | None, after: datetime) -> datetime | None:
    """When a recurring reminder should next fire, or None if it is one-shot."""
    if not recurrence:
        return None
    if recurrence == "hourly":
        return after + timedelta(hours=1)
    if recurrence == "daily":
        return after + timedelta(days=1)
    if recurrence == "weekly":
        return after + timedelta(weeks=1)
    if recurrence == "monthly":
        return after + timedelta(days=30)
    if recurrence == "weekdays":
        nxt = after + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt
    match = re.fullmatch(r"every:(\d+)([a-z]+)", recurrence)
    if match and match.group(2) in _UNITS:
        return after + timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})
    return None


class ReminderScheduler:
    """A daemon thread that fires due reminders through a callback."""

    def __init__(
        self,
        store: MongoStore,
        on_fire: Callable[[Reminder], Any],
        poll_seconds: int = 20,
    ) -> None:
        self.store = store
        self.on_fire = on_fire
        self.poll_seconds = max(1, poll_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-reminders", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # a bad callback must not kill the scheduler
                pass
            self._stop.wait(self.poll_seconds)

    def tick(self, now: datetime | None = None) -> list[Reminder]:
        """Fire everything due. Returns what fired - handy in tests."""
        moment = now or utcnow()
        fired: list[Reminder] = []
        for reminder in self.store.due_reminders(moment):
            self.store.complete_reminder(
                reminder.id, next_occurrence(reminder.recurrence, moment)
            )
            fired.append(reminder)
            try:
                self.on_fire(reminder)
            except Exception:
                pass
        return fired
