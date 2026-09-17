"""A session: one conversation, its history, its tools, and its reminders.

Interfaces own a Session and iterate the events it yields. Everything stateful
- the store, the task pool, the scheduler - lives here so the faces stay thin.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import anthropic

from .agent import Agent
from .audit import AuditLog, audit_enabled, default_path
from .config import NO_CREDENTIALS, JarvisConfig, credentials_available
from .errors import ConfigError
from .events import Event, TurnFinished
from .heartbeat import Heartbeat, as_event
from .memory import MongoStore, Notice
from .prompts import system_blocks
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

        self.audit = AuditLog(
            default_path(config.data_dir),
            session_id=self.session_id,
            enabled=config.audit and audit_enabled(),
        )
        self.audit.record("session", interface=interface, model=config.model, workspace=str(
            config.workspace
        ))

        owner_name = owner or self._remembered_owner()
        self.context = ToolContext(
            config=config,
            store=self.store,
            client=self.client,
            tasks=self.tasks,
            confirm=confirm,
            audit=self.audit,
        )
        self.registry = build_registry(config)
        self.agent = Agent(
            client=self.client,
            config=config,
            registry=self.registry,
            context=self.context,
            system=system_blocks(config, self.store, interface, owner_name),
        )
        self.heartbeat: Heartbeat | None = None

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

    # -- the heartbeat -------------------------------------------------
    def start_heartbeat(self, on_event: Callable[[Event], None] | None = None) -> Heartbeat:
        """Start the background loop, delivering anything it surfaces as events.

        Without a listener the loop still runs - it just holds everything it
        raises for whoever attaches next.
        """
        self.heartbeat = Heartbeat(self.config, self.store, on_event, audit=self.audit)
        self.heartbeat.start()
        return self.heartbeat

    # The old name, kept so existing interfaces and tests keep working.
    start_reminders = start_heartbeat

    def catch_up(self) -> list[Event]:
        """Everything raised while nobody was attached, for showing on return.

        Only what earned an interruption comes back this way. Quiet notices stay
        in the inbox for `/notices` - showing the lot on every startup is how a
        proactive assistant turns into noise you learn to skip.
        """
        held = [
            notice
            for notice in self.store.pending_notices(limit=50)
            if notice.level in {"notify", "urgent"}
        ]
        if not held:
            return []
        self.store.mark_delivered([notice.id for notice in held])
        return [as_event(notice) for notice in held]

    def open_notices(self, limit: int = 50) -> list[Notice]:
        """The inbox: everything surfaced and not yet dismissed."""
        return self.store.open_notices(limit=limit)

    # -- the kill switch -----------------------------------------------
    @property
    def paused(self) -> bool:
        return self.store.is_paused()

    def set_paused(self, paused: bool) -> None:
        """Stop or resume all proactive behaviour. The conversation is unaffected."""
        self.store.set_paused(paused)
        self.audit.record("kill_switch", paused=paused)

    # -- lifecycle -----------------------------------------------------
    def close(self) -> None:
        if self.heartbeat:
            # Detach first: anything raised on the way out is held, not dropped.
            self.heartbeat.detach()
            self.heartbeat.stop()
        self.tasks.shutdown()
        self.audit.record("session_end", **self.audit.totals.as_dict())
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
