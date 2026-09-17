"""A session: one conversation, its history, its tools, and its reminders.

Interfaces own a Session and iterate the events it yields. Everything stateful
- the store, the task pool, the scheduler - lives here so the faces stay thin.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import anthropic

from .agent import Agent
from .config import NO_CREDENTIALS, JarvisConfig, credentials_available
from .errors import ConfigError
from .events import Event, ReminderFired, TurnFinished
from .memory import MongoStore, Reminder
from .prompts import system_blocks
from .reminders import ReminderScheduler
from .tasks import TaskRegistry
from .toolkit import build_registry
from .tools.base import ToolContext


class Session:
    """One live conversation with Jarvis."""

    def __init__(
        self,
        config: JarvisConfig,
        interface: str = "cli",
        owner: str | None = None,
        confirm: Callable[[str, str], bool] | None = None,
        client: Any = None,
        store: MongoStore | None = None,
        persist: bool = True,
    ) -> None:
        config.ensure_dirs()
        self.config = config
        self.interface = interface
        self.persist = persist
        # Only close what we opened: an injected store belongs to the caller.
        self._owns_store = store is None
        self.store = store or MongoStore(config.mongodb_uri, config.mongodb_db)
        if self._owns_store:
            # Fail here, with a readable message, rather than mid-turn.
            self.store.ping()
        if client is None and not credentials_available():
            raise ConfigError(NO_CREDENTIALS)
        self.client = client or anthropic.Anthropic()
        self.tasks = TaskRegistry()
        self.messages: list[dict[str, Any]] = []
        self.session_id = self.store.create_session(interface=interface)

        owner_name = owner or self._remembered_owner()
        self.context = ToolContext(
            config=config,
            store=self.store,
            client=self.client,
            tasks=self.tasks,
            confirm=confirm,
        )
        self.registry = build_registry(config)
        self.agent = Agent(
            client=self.client,
            config=config,
            registry=self.registry,
            context=self.context,
            system=system_blocks(config, self.store, interface, owner_name),
        )
        self.scheduler: ReminderScheduler | None = None

    def _remembered_owner(self) -> str:
        for fact in self.store.recall("name", limit=5):
            if fact.key in {"name", "owner", "user.name"}:
                return fact.value
        return "the user"

    # -- conversation --------------------------------------------------
    def send(self, text: str) -> Iterator[Event]:
        """Send a message and stream the events of the resulting turn."""
        self.messages.append({"role": "user", "content": text})
        if self.persist:
            self.store.add_message(self.session_id, "user", text)

        before = len(self.messages)
        for event in self.agent.run(self.messages):
            yield event
            if isinstance(event, TurnFinished) and self.persist:
                for message in self.messages[before:]:
                    self.store.add_message(
                        self.session_id, message["role"], _serializable(message["content"])
                    )

    def interrupt(self) -> None:
        self.agent.cancel()

    def reset(self) -> None:
        """Start a fresh conversation, keeping memory and reminders."""
        self.messages = []
        self.session_id = self.store.create_session(interface=self.interface)

    # -- reminders -----------------------------------------------------
    def start_reminders(self, on_fire: Callable[[Event], None]) -> None:
        """Run the scheduler, delivering fired reminders as events."""
        def deliver(reminder: Reminder) -> None:
            on_fire(
                ReminderFired(
                    reminder_id=reminder.id, text=reminder.text, due_at=reminder.due_at
                )
            )

        self.scheduler = ReminderScheduler(
            self.store, deliver, poll_seconds=self.config.reminder_poll_seconds
        )
        self.scheduler.start()

    # -- lifecycle -----------------------------------------------------
    def close(self) -> None:
        if self.scheduler:
            self.scheduler.stop()
        self.tasks.shutdown()
        if self._owns_store:
            self.store.close()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _serializable(content: Any) -> Any:
    """Turn SDK content blocks into something JSON can hold."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return [_serializable(item) for item in content]
    if isinstance(content, dict):
        return {key: _serializable(value) for key, value in content.items()}
    dump = getattr(content, "to_dict", None) or getattr(content, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:
            pass
    return str(content)
