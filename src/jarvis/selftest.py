"""`jarvis selftest` - prove the durable half works against your real database.

Everything here needs a MongoDB and nothing here needs an API key: it exercises
memory, reminders, notices, the check schedule and the kill switch against a
real server, and says which ones worked.

It exists because the test suite runs on mongomock, which is an emulation.
Emulations differ from the real thing in exactly the places that matter -
whether `{"field": None}` matches a missing field, whether find_one_and_update
is really atomic - and those are the semantics the heartbeat's overlap guard is
built on. This is the check that runs on the server you will actually use.

It writes to a scratch database of its own and drops it afterwards, so it never
touches what Jarvis remembers.
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any

from .config import JarvisConfig
from .heartbeat import Heartbeat, should_interrupt
from .memory import MongoStore, StorageError, redact_uri, utcnow

SCRATCH_SUFFIX = "_selftest"


class Result:
    """What happened, so the caller can print it however it likes."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, label: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((label, ok, detail))
        return ok

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.rows)


def run(config: JarvisConfig, client: Any = None) -> Result:
    """Exercise everything durable. Returns a Result; never raises for a failure."""
    result = Result()
    scratch = f"{config.mongodb_db}{SCRATCH_SUFFIX}"

    try:
        store = MongoStore(config.mongodb_uri, scratch, client=client)
        store.ping()
        result.add("connect", True, f"{redact_uri(config.mongodb_uri)} -> {scratch}")
    except (StorageError, Exception) as exc:  # noqa: B014 - StorageError is an Exception
        result.add("connect", False, str(exc).splitlines()[0])
        return result

    try:
        _memory(store, result, config, client, scratch)
        _reminders(store, result, config)
        _notices(store, result, config)
        _schedule(store, result)
        _kill_switch(store, result, config, client, scratch)
    finally:
        try:
            store.db.client.drop_database(scratch)
            result.add("cleanup", True, f"dropped {scratch}")
        except Exception as exc:  # noqa: BLE001
            result.add("cleanup", False, f"could not drop {scratch}: {exc}")
    return result


def _memory(
    store: MongoStore, result: Result, config: JarvisConfig, client: Any, scratch: str
) -> None:
    store.remember("selftest.owner", "Isa", "identity")
    # A second store object, as a restart would build: this is the actual claim
    # Tier 4 makes, and reading back through the same object would not test it.
    fresh = MongoStore(config.mongodb_uri, scratch, client=client, ensure_indexes=False)
    facts = {f.key: f.value for f in fresh.recall("selftest")}
    result.add(
        "memory survives a restart",
        facts.get("selftest.owner") == "Isa",
        "wrote a fact, read it back through a new connection",
    )
    store.remember("selftest.owner", "Isa Moma", "identity")
    again = {f.key: f.value for f in fresh.recall("selftest")}
    result.add(
        "memory is editable",
        again.get("selftest.owner") == "Isa Moma" and len(again) == 1,
        "the same key updates in place rather than duplicating",
    )
    result.add("memory is deletable", store.forget("selftest.owner"), "")


def _reminders(store: MongoStore, result: Result, config: JarvisConfig) -> None:
    store.add_reminder("selftest reminder", utcnow() - timedelta(seconds=5))
    delivered: list[Any] = []
    beat = Heartbeat(config, store, delivered.append)
    beat.quiet_window = None      # this tests delivery, not what time it is
    beat.checks = []
    fired = beat.tick()
    result.add(
        "a due reminder fires",
        any("selftest reminder" in n.text for n in fired),
        f"{len(fired)} raised, {len(delivered)} delivered to the interface",
    )
    store.reminders.delete_many({})


def _notices(store: MongoStore, result: Result, config: JarvisConfig) -> None:
    store.notices.delete_many({})
    # Nothing attached: exactly the case where a proactive feature loses things.
    headless = Heartbeat(config, store)
    headless.checks = []
    headless.quiet_window = None
    headless.surface("selftest", "something happened while you were away", level="notify")

    held = [n for n in store.pending_notices() if n.source == "selftest"]
    result.add(
        "a notice raised with nobody watching is held",
        len(held) == 1 and held[0].status == "pending",
        "not delivered, not lost",
    )

    store.mark_delivered([n.id for n in held])
    still_open = [n for n in store.open_notices() if n.source == "selftest"]
    result.add(
        "seeing a notice does not clear it",
        len(still_open) == 1 and still_open[0].status == "delivered",
        "it stays in the inbox until dismissed",
    )
    result.add(
        "catch-up delivers once, not forever",
        not [n for n in store.pending_notices() if n.source == "selftest"],
        "",
    )

    headless.surface("selftest", "something happened while you were away", level="notify")
    repeats = [n for n in store.open_notices() if n.source == "selftest"]
    result.add(
        "a repeat is counted, not re-announced",
        len(repeats) == 1 and repeats[0].repeats == 1,
        "the same still-true condition does not interrupt twice",
    )

    result.add("a notice can be dismissed", store.dismiss_notice(repeats[0].id), "")
    # Fixed window, not the wall clock: 00:00-23:59 is always "quiet".
    night = (time(0, 0), time(23, 59))
    moment = utcnow().astimezone()
    result.add(
        "quiet hours hold back what is not urgent",
        not should_interrupt("notify", moment, night)
        and should_interrupt("urgent", moment, night),
        "notify waits for morning, urgent still gets through",
    )
    store.notices.delete_many({})


def _schedule(store: MongoStore, result: Result) -> None:
    """The overlap guard, against real server semantics rather than an emulation."""
    name = "selftest-check"
    store.checks.delete_many({})
    store.schedule_check(name, utcnow() - timedelta(seconds=1))

    # The document has no `running_since` field at all here. Whether
    # {"running_since": None} matches a *missing* field is precisely the kind of
    # thing an emulation can get wrong, and the whole claim depends on it.
    first = store.claim_check(name, utcnow())
    second = store.claim_check(name, utcnow())
    result.add(
        "a due check can be claimed",
        first,
        "matched on a field that does not exist yet",
    )
    result.add(
        "a second claim is refused while it runs",
        not second,
        "two heartbeats cannot run the same check at once",
    )

    store.release_check(name, utcnow() + timedelta(hours=1), status="ok", fingerprint="abc")
    state = store.check_state(name)
    result.add(
        "the schedule survives a restart",
        state.get("running_since") is None and state.get("next_due") > utcnow(),
        "next due time is stored, not held in memory",
    )
    result.add(
        "a check that is not due is not claimed",
        not store.claim_check(name, utcnow()),
        "",
    )

    stale = utcnow() - timedelta(hours=2)
    store.checks.update_one(
        {"_id": name}, {"$set": {"running_since": stale, "next_due": utcnow() - timedelta(1)}}
    )
    result.add(
        "a crashed run does not wedge a check forever",
        store.claim_check(name, utcnow()),
        "a claim older than the lease is retried",
    )
    store.checks.delete_many({})


def _kill_switch(
    store: MongoStore, result: Result, config: JarvisConfig, client: Any, scratch: str
) -> None:
    store.set_paused(True)
    elsewhere = MongoStore(config.mongodb_uri, scratch, client=client, ensure_indexes=False)
    result.add(
        "the kill switch is shared and durable",
        elsewhere.is_paused(),
        "the terminal, the browser and the VPS all see the same pause",
    )
    store.set_paused(False)
    result.add("it resumes", not elsewhere.is_paused(), "")
