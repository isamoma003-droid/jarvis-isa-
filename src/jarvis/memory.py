"""Persistent memory: what Jarvis keeps between runs.

Backed by MongoDB. One database (default `jarvis`):

| Collection | Holds |
|---|---|
| `facts`     | what Jarvis knows about you, keyed and upserted |
| `sessions`  | one document per conversation |
| `messages`  | the transcripts |
| `reminders` | pending, done and cancelled, with recurrence |
| `notices`   | the inbox: what the heartbeat surfaced, and whether you saw it |
| `checks`    | when each scheduled check last ran and is next due |
| `settings`  | the kill switch, and anything else every interface must agree on |

plus a `counters` document handing out small integer ids - "cancel reminder 3"
and "dismiss notice 4" are sayable out loud in a way an ObjectId is not.

The last three are what make the heartbeat durable: the schedule survives a
restart, a notice raised while you were away is still here when you return, and
pausing in the terminal is seen by the browser.

Times are stored as native BSON dates in UTC so range queries and indexes work
properly, and handed back to callers as ISO strings.
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import PyMongoError

from .errors import JarvisError

DEFAULT_URI = "mongodb://localhost:27017"
DEFAULT_DB = "jarvis"
# Fail fast and say something useful instead of hanging on an unreachable Atlas.
SERVER_SELECTION_TIMEOUT_MS = 5000


class StorageError(JarvisError):
    """The database could not be reached or a write failed."""


# One client per process, shared across sessions: pymongo pools connections
# internally, and a client per web-socket connection would exhaust the
# connection limit on a small Atlas tier. Cleared by the test suite.
_CLIENTS: dict[str, Any] = {}
_INDEXED: set[str] = set()
_CLIENT_LOCK = threading.Lock()


def shared_client(uri: str) -> Any:
    """The process-wide client for this URI, created on first use."""
    with _CLIENT_LOCK:
        client = _CLIENTS.get(uri)
        if client is None:
            client = MongoClient(
                uri,
                tz_aware=True,
                serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
                appname="jarvis",
            )
            _CLIENTS[uri] = client
        return client


def close_clients() -> None:
    """Close every shared client. For process shutdown and tests."""
    with _CLIENT_LOCK:
        for client in _CLIENTS.values():
            client.close()
        _CLIENTS.clear()
        _INDEXED.clear()


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    """Serialize to UTC ISO-8601. Naive input is assumed to be local time."""
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _utc(moment: datetime) -> datetime:
    """Normalize to an aware UTC datetime, for storing and comparing."""
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(UTC)


def _as_iso(value: Any) -> str:
    """A BSON date back out as an ISO string. Tolerates a stored string."""
    if isinstance(value, datetime):
        return iso(value)
    return str(value) if value else ""


@dataclass(slots=True)
class Fact:
    key: str
    value: str
    category: str
    updated_at: str


@dataclass(slots=True)
class Reminder:
    id: int
    text: str
    due_at: str
    recurrence: str | None
    status: str
    created_at: str
    fired_at: str | None = None

    @property
    def due(self) -> datetime:
        return datetime.fromisoformat(self.due_at)


@dataclass(slots=True)
class Notice:
    """Something Jarvis noticed on its own and wants me to see.

    `level` decides how loudly: `quiet` waits in the inbox, `notify` interrupts
    during waking hours, `urgent` interrupts regardless. `status` is the delivery
    record - a notice raised while nothing was attached stays `pending` until an
    interface picks it up, which is how nothing gets lost while I'm away.
    """

    id: int
    source: str
    text: str
    level: str
    status: str
    created_at: str
    detail: str = ""
    delivered_at: str | None = None
    dismissed_at: str | None = None
    # How many further times the same condition was seen while this was open.
    repeats: int = 0


NOTICE_LEVELS = ("quiet", "notify", "urgent")


def _fact(doc: dict[str, Any]) -> Fact:
    return Fact(
        key=doc["_id"],
        value=doc.get("value", ""),
        category=doc.get("category", "general"),
        updated_at=_as_iso(doc.get("updated_at")),
    )


def _notice(doc: dict[str, Any]) -> Notice:
    return Notice(
        id=doc["_id"],
        source=doc.get("source", "jarvis"),
        text=doc.get("text", ""),
        detail=doc.get("detail", ""),
        level=doc.get("level", "quiet"),
        status=doc.get("status", "pending"),
        created_at=_as_iso(doc.get("created_at")),
        delivered_at=_as_iso(doc.get("delivered_at")) or None,
        dismissed_at=_as_iso(doc.get("dismissed_at")) or None,
        repeats=int(doc.get("repeats") or 0),
    )


def _reminder(doc: dict[str, Any]) -> Reminder:
    return Reminder(
        id=doc["_id"],
        text=doc.get("text", ""),
        due_at=_as_iso(doc.get("due_at")),
        recurrence=doc.get("recurrence"),
        status=doc.get("status", "pending"),
        created_at=_as_iso(doc.get("created_at")),
        fired_at=_as_iso(doc.get("fired_at")) or None,
    )


class MongoStore:
    """Everything Jarvis keeps between runs."""

    def __init__(
        self,
        uri: str = DEFAULT_URI,
        db_name: str = DEFAULT_DB,
        client: Any = None,
        ensure_indexes: bool = True,
    ) -> None:
        self.uri = uri
        self.db_name = db_name
        self._client = client or shared_client(uri)
        self.db = self._client[db_name]
        self.facts = self.db["facts"]
        self.sessions = self.db["sessions"]
        self.messages = self.db["messages"]
        self.reminders = self.db["reminders"]
        self.counters = self.db["counters"]
        self.notices = self.db["notices"]
        self.checks = self.db["checks"]
        self.settings = self.db["settings"]
        if ensure_indexes:
            self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        """Once per process per database - not on every session.

        Idempotent, and a missing index is not worth refusing to start over.
        """
        marker = f"{self.uri}/{self.db_name}"
        with _CLIENT_LOCK:
            if marker in _INDEXED:
                return
            _INDEXED.add(marker)
        try:
            self.facts.create_index([("updated_at", DESCENDING)])
            self.facts.create_index([("category", ASCENDING)])
            self.sessions.create_index([("updated_at", DESCENDING)])
            self.messages.create_index([("session_id", ASCENDING), ("_id", ASCENDING)])
            self.reminders.create_index([("status", ASCENDING), ("due_at", ASCENDING)])
            self.notices.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
            self.checks.create_index([("next_due", ASCENDING)])
        except PyMongoError:
            pass

    def ping(self) -> None:
        """Raise StorageError unless the server answers."""
        try:
            self._client.admin.command("ping")
        except PyMongoError as exc:
            raise StorageError(unreachable_message(self.uri, exc)) from exc

    def close(self) -> None:
        """A no-op by design: the client is shared process-wide.

        Closing here would pull the connection pool out from under every other
        live session. Use close_clients() at process shutdown instead.
        """
        return None

    def _next_id(self, name: str) -> int:
        """A small monotonic integer, the standard Mongo counter pattern."""
        doc = self.counters.find_one_and_update(
            {"_id": name},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(doc["seq"])

    # -- facts ---------------------------------------------------------
    def remember(self, key: str, value: str, category: str = "general") -> Fact:
        key = key.strip().lower()
        if not key:
            raise ValueError("a fact needs a key")
        now = utcnow()
        self.facts.update_one(
            {"_id": key},
            {
                "$set": {"value": value, "category": category, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return Fact(key=key, value=value, category=category, updated_at=iso(now))

    def recall(self, query: str = "", limit: int = 50) -> list[Fact]:
        criteria: dict[str, Any] = {}
        if query.strip():
            # Escaped: a remembered value is not a regular expression.
            pattern = re.compile(re.escape(query.strip()), re.IGNORECASE)
            criteria = {
                "$or": [
                    {"_id": pattern},
                    {"value": pattern},
                    {"category": pattern},
                ]
            }
        cursor = self.facts.find(criteria).sort("updated_at", DESCENDING).limit(limit)
        return [_fact(doc) for doc in cursor]

    def forget(self, key: str) -> bool:
        return self.facts.delete_one({"_id": key.strip().lower()}).deleted_count > 0

    # -- sessions and transcripts --------------------------------------
    def create_session(self, interface: str = "cli", title: str | None = None) -> str:
        session_id = uuid.uuid4().hex[:12]
        now = utcnow()
        self.sessions.insert_one({
            "_id": session_id,
            "title": title,
            "interface": interface,
            "created_at": now,
            "updated_at": now,
        })
        return session_id

    def add_message(self, session_id: str, role: str, content: Any) -> None:
        now = utcnow()
        self.messages.insert_one({
            "session_id": session_id,
            "role": role,
            "content": content,
            "created_at": now,
        })
        self.sessions.update_one({"_id": session_id}, {"$set": {"updated_at": now}})

    def session_messages(self, session_id: str) -> list[dict[str, Any]]:
        # Sorted by _id: an ObjectId is monotonic within the process that wrote
        # it, and one conversation is only ever written by one process.
        cursor = self.messages.find({"session_id": session_id}).sort("_id", ASCENDING)
        return [{"role": doc["role"], "content": doc["content"]} for doc in cursor]

    def recent_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        cursor = self.sessions.find().sort("updated_at", DESCENDING).limit(limit)
        return [
            {
                "id": doc["_id"],
                "title": doc.get("title"),
                "interface": doc.get("interface", "cli"),
                "created_at": _as_iso(doc.get("created_at")),
                "updated_at": _as_iso(doc.get("updated_at")),
            }
            for doc in cursor
        ]

    # -- reminders -----------------------------------------------------
    def add_reminder(self, text: str, due_at: datetime, recurrence: str | None = None) -> Reminder:
        now = utcnow()
        doc = {
            "_id": self._next_id("reminders"),
            "text": text,
            "due_at": _utc(due_at),
            "recurrence": recurrence,
            "status": "pending",
            "created_at": now,
            "fired_at": None,
        }
        self.reminders.insert_one(doc)
        return _reminder(doc)

    def due_reminders(self, now: datetime | None = None) -> list[Reminder]:
        cursor = self.reminders.find(
            {"status": "pending", "due_at": {"$lte": _utc(now or utcnow())}}
        ).sort("due_at", ASCENDING)
        return [_reminder(doc) for doc in cursor]

    def list_reminders(self, status: str = "pending", limit: int = 50) -> list[Reminder]:
        criteria = {} if status == "all" else {"status": status}
        cursor = self.reminders.find(criteria).sort("due_at", ASCENDING).limit(limit)
        return [_reminder(doc) for doc in cursor]

    def get_reminder(self, reminder_id: int) -> Reminder | None:
        doc = self.reminders.find_one({"_id": reminder_id})
        return _reminder(doc) if doc else None

    def complete_reminder(self, reminder_id: int, next_due: datetime | None = None) -> None:
        """Mark fired. A recurring reminder is rescheduled instead of closed."""
        now = utcnow()
        if next_due is not None:
            update = {"$set": {"due_at": _utc(next_due), "fired_at": now}}
        else:
            update = {"$set": {"status": "done", "fired_at": now}}
        self.reminders.update_one({"_id": reminder_id}, update)

    def cancel_reminder(self, reminder_id: int) -> bool:
        result = self.reminders.update_one(
            {"_id": reminder_id, "status": "pending"}, {"$set": {"status": "cancelled"}}
        )
        return result.modified_count > 0

    # -- notices -------------------------------------------------------
    def add_notice(
        self, source: str, text: str, detail: str = "", level: str = "quiet"
    ) -> Notice:
        """Record something worth my attention. Held until an interface takes it."""
        if level not in NOTICE_LEVELS:
            raise ValueError(f"level must be one of {NOTICE_LEVELS}, got {level!r}")
        doc = {
            "_id": self._next_id("notices"),
            "source": source,
            "text": text,
            "detail": detail,
            "level": level,
            "status": "pending",
            "created_at": utcnow(),
            "delivered_at": None,
            "dismissed_at": None,
        }
        self.notices.insert_one(doc)
        return _notice(doc)

    def pending_notices(self, limit: int = 50) -> list[Notice]:
        """Raised but never shown to anyone. This is the catch-up queue."""
        cursor = (
            self.notices.find({"status": "pending"}).sort("created_at", ASCENDING).limit(limit)
        )
        return [_notice(doc) for doc in cursor]

    def open_notices(self, limit: int = 50) -> list[Notice]:
        """Everything not yet dismissed - the inbox, delivered or not."""
        cursor = (
            self.notices.find({"status": {"$ne": "dismissed"}})
            .sort("created_at", ASCENDING)
            .limit(limit)
        )
        return [_notice(doc) for doc in cursor]

    def get_notice(self, notice_id: int) -> Notice | None:
        doc = self.notices.find_one({"_id": notice_id})
        return _notice(doc) if doc else None

    def duplicate_notice(self, source: str, text: str) -> Notice | None:
        """An open notice already saying exactly this, if there is one."""
        doc = self.notices.find_one(
            {"source": source, "text": text, "status": {"$ne": "dismissed"}}
        )
        return _notice(doc) if doc else None

    def bump_notice(self, notice_id: int) -> None:
        """Count another occurrence of something already in the inbox."""
        self.notices.update_one(
            {"_id": notice_id}, {"$inc": {"repeats": 1}, "$set": {"last_seen": utcnow()}}
        )

    def mark_delivered(self, notice_ids: list[int]) -> None:
        """Shown to someone. Still open until dismissed - seeing is not clearing."""
        if not notice_ids:
            return
        self.notices.update_many(
            {"_id": {"$in": list(notice_ids)}, "status": "pending"},
            {"$set": {"status": "delivered", "delivered_at": utcnow()}},
        )

    def dismiss_notice(self, notice_id: int) -> bool:
        result = self.notices.update_one(
            {"_id": notice_id, "status": {"$ne": "dismissed"}},
            {"$set": {"status": "dismissed", "dismissed_at": utcnow()}},
        )
        return result.modified_count > 0

    def dismiss_all_notices(self) -> int:
        result = self.notices.update_many(
            {"status": {"$ne": "dismissed"}},
            {"$set": {"status": "dismissed", "dismissed_at": utcnow()}},
        )
        return int(result.modified_count)

    # -- the check schedule --------------------------------------------
    def check_state(self, name: str) -> dict[str, Any]:
        return self.checks.find_one({"_id": name}) or {}

    def all_check_states(self) -> list[dict[str, Any]]:
        return list(self.checks.find().sort("_id", ASCENDING))

    def schedule_check(self, name: str, next_due: datetime) -> None:
        """Give a check a first due time, without disturbing one it already has.

        `$setOnInsert` is what makes a restart resume the schedule rather than
        resetting every timer and firing the lot on boot.
        """
        self.checks.update_one(
            {"_id": name},
            {"$setOnInsert": {"next_due": _utc(next_due), "last_run": None, "last_status": ""}},
            upsert=True,
        )

    def claim_check(self, name: str, now: datetime, lease_seconds: int = 900) -> bool:
        """Atomically take ownership of a due check. False means: not mine to run.

        One statement does both jobs - "is it due?" and "is another run still
        going?" - so two heartbeats (say a laptop and a VPS on the same database)
        cannot both pick up the same check. The lease bounds a run that died
        without releasing; without it a crash would wedge the check forever.
        """
        moment = _utc(now)
        stale = moment - timedelta(seconds=lease_seconds)
        result = self.checks.find_one_and_update(
            {
                "_id": name,
                "next_due": {"$lte": moment},
                "$or": [{"running_since": None}, {"running_since": {"$lte": stale}}],
            },
            {"$set": {"running_since": moment}},
        )
        return result is not None

    def release_check(
        self,
        name: str,
        next_due: datetime,
        status: str = "ok",
        fingerprint: str | None = None,
    ) -> None:
        """Record the outcome and when the check is next due."""
        update: dict[str, Any] = {
            "next_due": _utc(next_due),
            "last_run": utcnow(),
            "last_status": status,
            "running_since": None,
        }
        if fingerprint is not None:
            update["fingerprint"] = fingerprint
        self.checks.update_one({"_id": name}, {"$set": update}, upsert=True)

    def forget_checks(self, keep: list[str]) -> int:
        """Drop schedule state for checks no longer in the config."""
        result = self.checks.delete_many({"_id": {"$nin": list(keep)}})
        return int(result.deleted_count)

    # -- settings ------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        doc = self.settings.find_one({"_id": key})
        return doc.get("value", default) if doc else default

    def set_setting(self, key: str, value: Any) -> None:
        self.settings.update_one(
            {"_id": key}, {"$set": {"value": value, "updated_at": utcnow()}}, upsert=True
        )

    def is_paused(self) -> bool:
        """The kill switch. Durable, so it survives a restart and every interface
        sees the same answer."""
        return bool(self.get_setting("paused", False))

    def set_paused(self, paused: bool) -> None:
        self.set_setting("paused", bool(paused))


def redact_uri(uri: str) -> str:
    """A connection string safe to print: the password never appears."""
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", uri)


def unreachable_message(uri: str, exc: Exception) -> str:
    """What to tell a person whose database is not answering."""
    # pymongo's message carries a full topology dump; keep the first clause.
    reason = str(exc).split("(configured timeouts")[0].split(", Timeout:")[0]
    reason = reason.strip().rstrip(",") or type(exc).__name__
    return (
        f"Could not reach MongoDB at {redact_uri(uri)} - {reason}.\n"
        "Check MONGODB_URI, that the cluster is awake, and that this machine's "
        "IP is allowed in Atlas. `jarvis doctor` tests the connection."
    )
