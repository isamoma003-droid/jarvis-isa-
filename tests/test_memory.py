"""The store, the reminder scheduler, and the background task registry."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jarvis.memory import MongoStore, utcnow
from jarvis.reminders import (
    ReminderScheduler,
    TimeParseError,
    next_occurrence,
    parse_recurrence,
    parse_when,
)
from jarvis.tasks import TaskRegistry


def test_facts_outlive_the_store_object(mongo_client):
    """A fact lives in the database, not in the Python object that wrote it."""
    first = MongoStore(client=mongo_client)
    first.remember("name", "Isa", "identity")
    first.close()

    reopened = MongoStore(client=mongo_client)
    assert [f.value for f in reopened.recall()] == ["Isa"]


def test_reminder_ids_are_small_integers(store):
    """Sayable out loud: "cancel reminder 2", not an ObjectId."""
    first = store.add_reminder("one", utcnow())
    second = store.add_reminder("two", utcnow())
    assert (first.id, second.id) == (1, 2)


def test_a_search_term_is_not_treated_as_a_regex(store):
    store.remember("editor", "neovim", "preference")
    assert store.recall(".*") == []      # would match everything if unescaped
    assert store.recall("neo(vim") == []  # would raise if unescaped


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


def test_one_client_is_shared_across_stores(mongo_client):
    """A client per session would exhaust a small Atlas tier's connections."""
    import jarvis.memory as memory

    first = MongoStore("mongodb://localhost:27017")
    second = MongoStore("mongodb://localhost:27017")
    assert first._client is second._client
    assert len(memory._CLIENTS) == 1

    # closing one session must not pull the pool out from under the other
    first.close()
    second.remember("still", "working")
    assert [f.key for f in second.recall()] == ["still"]


def test_indexes_are_built_once_per_process(mongo_client):
    import jarvis.memory as memory

    MongoStore("mongodb://localhost:27017")
    MongoStore("mongodb://localhost:27017")
    assert memory._INDEXED == {"mongodb://localhost:27017/jarvis"}


def test_an_unreachable_server_is_explained_not_dumped():
    """pymongo's own error is a topology dump; a person needs one sentence."""
    from pymongo.errors import ServerSelectionTimeoutError

    from jarvis.memory import unreachable_message

    exc = ServerSelectionTimeoutError(
        "127.0.0.1:27099: [Errno 111] Connection refused (configured timeouts: "
        "socketTimeoutMS: 20000.0ms), Timeout: 5.0s, Topology Description: <...>"
    )
    message = unreachable_message("mongodb+srv://isa:hunter2@cluster0.abc.mongodb.net/", exc)

    assert "Connection refused" in message
    assert "Topology Description" not in message
    assert "hunter2" not in message
    assert "jarvis doctor" in message


def test_ping_raises_a_jarvis_error(mongo_client):
    from pymongo.errors import ServerSelectionTimeoutError

    from jarvis.errors import JarvisError
    from jarvis.memory import StorageError

    class Dead:
        class admin:
            @staticmethod
            def command(*args, **kwargs):
                raise ServerSelectionTimeoutError("nope")

        def __getitem__(self, name):
            return mongo_client[name]

    store = MongoStore(client=Dead(), ensure_indexes=False)
    with pytest.raises(StorageError) as caught:
        store.ping()
    assert isinstance(caught.value, JarvisError)  # so the CLI renders it in red


# --- when the database is not there ----------------------------------
def test_a_local_database_that_is_not_running_gets_local_advice():
    from jarvis.memory import unreachable_message

    message = unreachable_message(
        "mongodb://localhost:27017", ConnectionRefusedError("[Errno 111] Connection refused")
    )
    assert "Nothing is listening" in message
    assert "docker run" in message
    assert "no `mongodb` package" in message   # apt will not help on Mint
    assert "IP is allowed in Atlas" not in message  # nothing is misconfigured


def test_a_remote_cluster_gets_cluster_advice():
    from jarvis.memory import unreachable_message

    message = unreachable_message(
        "mongodb+srv://isa:hunter2@cluster0.abc.mongodb.net/", TimeoutError("timed out")
    )
    assert "IP is allowed in Atlas" in message
    assert "docker run" not in message
    assert "hunter2" not in message            # the password never leaks into an error


def test_an_unreplaced_atlas_placeholder_is_named_before_dialling():
    """Atlas's copy button hands you a template. Leaving a placeholder in it is
    the commonest first-run mistake, and the driver's own error for it is a
    replica-set timeout that describes the symptom, not the cause."""
    from jarvis.memory import uri_problem

    problem = uri_problem("mongodb+srv://<db_username>:realpassword@cluster0.abc.mongodb.net/")
    assert problem is not None
    assert "<db_username>" in problem
    assert "placeholder" in problem
    assert "percent-encoded" in problem       # the next thing they will hit


def test_a_complete_connection_string_has_no_complaint():
    from jarvis.memory import uri_problem

    assert uri_problem("mongodb+srv://isa:s3cret@cluster0.abc.mongodb.net/") is None
    assert uri_problem("mongodb://localhost:27017") is None


def test_an_empty_or_malformed_uri_says_so():
    from jarvis.memory import uri_problem

    assert "No MongoDB connection string" in (uri_problem("") or "")
    assert "must start with" in (uri_problem("cluster0.abc.mongodb.net") or "")


def test_ping_refuses_a_placeholder_without_a_network_round_trip(mongo_client):
    from jarvis.memory import MongoStore, StorageError

    store = MongoStore(
        "mongodb+srv://<db_username>:pw@cluster0.abc.mongodb.net/",
        client=mongo_client,
        ensure_indexes=False,
    )
    with pytest.raises(StorageError, match="placeholder"):
        store.ping()
