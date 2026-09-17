"""The store, the reminder scheduler, and the background task registry."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jarvis.memory import Store, utcnow
from jarvis.reminders import (
    ReminderScheduler,
    TimeParseError,
    next_occurrence,
    parse_recurrence,
    parse_when,
)
from jarvis.tasks import TaskRegistry


def test_facts_survive_reopening(tmp_path):
    path = tmp_path / "jarvis.db"
    store = Store(path)
    store.remember("name", "Isa", "identity")
    store.close()

    reopened = Store(path)
    assert [f.value for f in reopened.recall()] == ["Isa"]
    reopened.close()


def test_recall_searches_key_value_and_category(store):
    store.remember("editor", "neovim", "preference")
    store.remember("repo", "jarvis-isa-", "project")

    assert len(store.recall("neovim")) == 1
    assert len(store.recall("preference")) == 1
    assert len(store.recall("")) == 2
    assert store.recall("nothing") == []


def test_transcripts_are_kept_per_session(store):
    first = store.create_session("cli")
    second = store.create_session("web")
    store.add_message(first, "user", "hello")
    store.add_message(first, "assistant", [{"type": "text", "text": "hi"}])
    store.add_message(second, "user", "other")

    assert len(store.session_messages(first)) == 2
    assert store.session_messages(first)[1]["content"] == [{"type": "text", "text": "hi"}]
    assert len(store.session_messages(second)) == 1
    assert len(store.recent_sessions()) == 2


# --- time parsing ----------------------------------------------------
@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("in 30 minutes", "2026-09-17 15:00"),
        ("in 2h", "2026-09-17 16:30"),
        ("tomorrow at 9am", "2026-09-18 09:00"),
        ("tomorrow", "2026-09-18 09:00"),
        ("today at 18:15", "2026-09-17 18:15"),
        ("tonight", "2026-09-17 20:00"),
        ("friday 17:00", "2026-09-18 17:00"),
        ("2026-12-01T08:30", "2026-12-01 08:30"),
        ("9am", "2026-09-18 09:00"),  # already passed today, so tomorrow
        ("23:45", "2026-09-17 23:45"),
    ],
)
def test_parse_when(phrase, expected):
    base = datetime(2026, 9, 17, 14, 30).astimezone()
    assert parse_when(phrase, base).strftime("%Y-%m-%d %H:%M") == expected


def test_parse_when_rejects_vagueness():
    with pytest.raises(TimeParseError):
        parse_when("soon-ish")


def test_recurrence_parsing():
    assert parse_recurrence("daily") == "daily"
    assert parse_recurrence("every 30 minutes") == "every:30minutes"
    assert parse_recurrence(None) is None
    assert parse_recurrence("once") is None
    with pytest.raises(TimeParseError):
        parse_recurrence("every other tuesday")


def test_next_occurrence_skips_the_weekend():
    friday = datetime(2026, 9, 18, 9, 0).astimezone()
    assert next_occurrence("weekdays", friday).strftime("%a") == "Mon"
    assert next_occurrence("every:45minutes", friday).strftime("%H:%M") == "09:45"
    assert next_occurrence(None, friday) is None


# --- scheduler -------------------------------------------------------
def test_scheduler_fires_due_reminders_once(store):
    fired = []
    store.add_reminder("past", utcnow() - timedelta(minutes=1))
    store.add_reminder("future", utcnow() + timedelta(hours=1))
    scheduler = ReminderScheduler(store, fired.append, poll_seconds=1)

    assert [r.text for r in scheduler.tick()] == ["past"]
    assert [r.text for r in fired] == ["past"]
    assert scheduler.tick() == []  # a one-shot reminder does not fire twice
    assert store.list_reminders("done")[0].text == "past"


def test_scheduler_reschedules_a_recurring_reminder(store):
    store.add_reminder("standup", utcnow() - timedelta(seconds=5), "daily")
    scheduler = ReminderScheduler(store, lambda r: None)

    assert len(scheduler.tick()) == 1
    assert scheduler.tick() == []
    pending = store.list_reminders("pending")
    assert len(pending) == 1
    assert pending[0].due > utcnow()


def test_a_broken_callback_does_not_stop_the_scheduler(store):
    store.add_reminder("one", utcnow() - timedelta(minutes=2))
    store.add_reminder("two", utcnow() - timedelta(minutes=1))

    def explode(reminder):
        raise RuntimeError("bad handler")

    scheduler = ReminderScheduler(store, explode)
    assert len(scheduler.tick()) == 2


def test_cancelled_reminders_never_fire(store):
    reminder = store.add_reminder("cancelled", utcnow() - timedelta(minutes=1))
    assert store.cancel_reminder(reminder.id) is True
    assert store.cancel_reminder(reminder.id) is False  # already cancelled

    scheduler = ReminderScheduler(store, lambda r: None)
    assert scheduler.tick() == []


# --- background tasks ------------------------------------------------
def test_task_registry_records_success_and_failure():
    registry = TaskRegistry(max_workers=2)

    good = registry.submit("good work", "coder", lambda task: "finished cleanly")
    bad = registry.submit("bad work", "coder", lambda task: 1 / 0)

    assert registry.wait(good.id, timeout=5).status == "done"
    assert "finished cleanly" in registry.get(good.id).summary()

    failed = registry.wait(bad.id, timeout=5)
    assert failed.status == "failed"
    assert "ZeroDivisionError" in failed.error
    assert len(registry.all()) == 2
    registry.shutdown(wait=True)


def test_task_progress_is_visible_while_running():
    registry = TaskRegistry(max_workers=1)
    import threading

    release = threading.Event()

    def slow(task):
        task.log.append("web_search")
        release.wait(timeout=5)
        return "done"

    task = registry.submit("slow work", "researcher", slow)
    assert registry.get(task.id).status == "running"
    assert "web_search" in registry.get(task.id).summary()
    release.set()
    assert registry.wait(task.id, timeout=5).status == "done"
    registry.shutdown(wait=True)
