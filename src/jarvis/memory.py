"""Persistent memory: facts Jarvis remembers, transcripts, and reminders.

One SQLite file under the data directory. The scheduler thread and the agent
thread share a connection, so every write goes through a lock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    category   TEXT NOT NULL DEFAULT 'general',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    title      TEXT,
    interface  TEXT NOT NULL DEFAULT 'cli',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    due_at     TEXT NOT NULL,
    recurrence TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    fired_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    """Serialize to UTC ISO-8601. Naive input is assumed to be local time."""
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(UTC).isoformat(timespec="seconds")


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


class Store:
    """Everything Jarvis keeps between runs."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- facts ---------------------------------------------------------
    def remember(self, key: str, value: str, category: str = "general") -> Fact:
        key = key.strip().lower()
        if not key:
            raise ValueError("a fact needs a key")
        now = iso(utcnow())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO facts (key, value, category, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    category = excluded.category,
                    updated_at = excluded.updated_at
                """,
                (key, value, category, now, now),
            )
            self._conn.commit()
        return Fact(key=key, value=value, category=category, updated_at=now)

    def recall(self, query: str = "", limit: int = 50) -> list[Fact]:
        sql = "SELECT key, value, category, updated_at FROM facts"
        params: tuple[Any, ...] = ()
        if query.strip():
            sql += " WHERE key LIKE ? OR value LIKE ? OR category LIKE ?"
            like = f"%{query.strip()}%"
            params = (like, like, like)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params += (limit,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Fact(**dict(row)) for row in rows]

    def forget(self, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM facts WHERE key = ?", (key.strip().lower(),))
            self._conn.commit()
        return cur.rowcount > 0

    # -- sessions and transcripts --------------------------------------
    def create_session(self, interface: str = "cli", title: str | None = None) -> str:
        session_id = uuid.uuid4().hex[:12]
        now = iso(utcnow())
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, title, interface, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, title, interface, now, now),
            )
            self._conn.commit()
        return session_id

    def add_message(self, session_id: str, role: str, content: Any) -> None:
        now = iso(utcnow())
        payload = json.dumps(content, default=str)
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, payload, now),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
            )
            self._conn.commit()

    def session_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [{"role": row["role"], "content": json.loads(row["content"])} for row in rows]

    def recent_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, title, interface, created_at, updated_at FROM sessions"
                " ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- reminders -----------------------------------------------------
    def add_reminder(self, text: str, due_at: datetime, recurrence: str | None = None) -> Reminder:
        now = iso(utcnow())
        due = iso(due_at)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reminders (text, due_at, recurrence, status, created_at)"
                " VALUES (?, ?, ?, 'pending', ?)",
                (text, due, recurrence, now),
            )
            self._conn.commit()
            reminder_id = int(cur.lastrowid or 0)
        return Reminder(
            id=reminder_id,
            text=text,
            due_at=due,
            recurrence=recurrence,
            status="pending",
            created_at=now,
        )

    def due_reminders(self, now: datetime | None = None) -> list[Reminder]:
        moment = iso(now or utcnow())
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ? ORDER BY due_at",
                (moment,),
            ).fetchall()
        return [Reminder(**dict(row)) for row in rows]

    def list_reminders(self, status: str = "pending", limit: int = 50) -> list[Reminder]:
        sql = "SELECT * FROM reminders"
        params: tuple[Any, ...] = ()
        if status != "all":
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY due_at LIMIT ?"
        params += (limit,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Reminder(**dict(row)) for row in rows]

    def get_reminder(self, reminder_id: int) -> Reminder | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
        return Reminder(**dict(row)) if row else None

    def complete_reminder(self, reminder_id: int, next_due: datetime | None = None) -> None:
        """Mark fired. A recurring reminder is rescheduled instead of closed."""
        now = iso(utcnow())
        with self._lock:
            if next_due is not None:
                self._conn.execute(
                    "UPDATE reminders SET due_at = ?, fired_at = ? WHERE id = ?",
                    (iso(next_due), now, reminder_id),
                )
            else:
                self._conn.execute(
                    "UPDATE reminders SET status = 'done', fired_at = ? WHERE id = ?",
                    (now, reminder_id),
                )
            self._conn.commit()

    def cancel_reminder(self, reminder_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                (reminder_id,),
            )
            self._conn.commit()
        return cur.rowcount > 0
