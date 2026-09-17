"""Tool plumbing: the context tools run in, the registry, and input validation."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ApprovalDenied, SandboxViolation
from ..events import Event

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from ..audit import AuditLog
    from ..config import JarvisConfig
    from ..memory import MongoStore
    from ..tasks import TaskRegistry

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


@dataclass
class ToolContext:
    """Everything a tool is allowed to touch."""

    config: JarvisConfig
    store: MongoStore
    client: Any = None
    tasks: TaskRegistry | None = None
    emit: Callable[[Event], None] = lambda event: None
    confirm: Callable[[str, str], bool] | None = None
    depth: int = 0
    audit: AuditLog | None = None
    # True when nothing with a human attached is driving this - a background
    # sub-agent, or a heartbeat-initiated action. Nothing here may block on an
    # answer that is never coming.
    unattended: bool = False
    _queue: deque[Event] = field(default_factory=deque, repr=False)

    def post(self, event: Event) -> None:
        """Queue an event for the agent loop to yield after the current tool."""
        self._queue.append(event)

    def drain(self) -> list[Event]:
        """Take everything queued since the last drain."""
        drained = list(self._queue)
        self._queue.clear()
        return drained

    @property
    def workspace(self) -> Path:
        return self.config.workspace

    # -- sandboxing ----------------------------------------------------
    def resolve(self, path: str, must_exist: bool = False) -> Path:
        """Resolve a model-supplied path inside the workspace, or refuse.

        Model output is untrusted: schema validation says a path is a string,
        not that it stays where it belongs.
        """
        candidate = Path(path).expanduser()
        target = (self.workspace / candidate).resolve() if not candidate.is_absolute() \
            else candidate.resolve()
        if not target.is_relative_to(self.workspace):
            raise SandboxViolation(
                f"{path!r} resolves outside the workspace ({self.workspace}); refused"
            )
        if must_exist and not target.exists():
            raise SandboxViolation(f"{path!r} does not exist")
        return target

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace)) or "."
        except ValueError:
            return str(path)

    # -- notices -------------------------------------------------------
    def notify(self, text: str, detail: str = "", level: str = "quiet") -> None:
        """Leave a note in the inbox. Never raises - a note is never the point."""
        try:
            self.store.add_notice(source="jarvis", text=text, detail=detail, level=level)
        except Exception:
            pass

    # -- approval ------------------------------------------------------
    def approve(self, action: str, detail: str, outward: bool = False) -> None:
        """Gate a side effect. Raises ApprovalDenied when the answer is no.

        This sits between the model choosing a tool and the tool running, so it
        covers typed, spoken and heartbeat-initiated actions identically.

        `outward` marks an action that reaches another person or another machine.
        Those ask **every time**: not waived by `approval = "auto"`, and never
        pre-authorised by a previous yes. Confirmation is per action and does not
        generalise.
        """
        policy = self.config.approval
        must_ask = outward and self.config.confirm_outward

        def refuse(reason: str, why: str) -> None:
            if self.audit is not None:
                self.audit.approval(action, detail, granted=False, why=why)
            raise ApprovalDenied(reason)

        if policy == "deny":
            refuse(
                f"{action} needs approval and the approval policy is 'deny'. "
                "Tell the user what you wanted to run and why.",
                "policy is deny",
            )
        if policy == "auto" and not must_ask:
            if self.audit is not None:
                self.audit.approval(action, detail, granted=True, why="approval policy is auto")
            return
        if self.unattended or self.confirm is None:
            # The safe default when there is nobody to ask: do nothing, and leave
            # a note. A background loop that blocks on an absent human stops
            # quietly, and you find out weeks later.
            note = "outward-facing action" if must_ask else "action needing approval"
            self.notify(
                f"held back: wanted to {action}",
                f"{detail}\n\nNot done - no one was available to approve this {note}.",
            )
            refuse(
                f"{action} needs your approval and nobody is attached to ask. "
                "Nothing was done; a note is waiting in the inbox.",
                "unattended",
            )
        granted = bool(self.confirm(action, detail))
        if self.audit is not None:
            self.audit.approval(action, detail, granted=granted, why="asked" if granted else "")
        if not granted:
            raise ApprovalDenied(f"the user declined: {action}")


@dataclass
class Tool:
    """A callable Claude can invoke, plus the schema it is described by."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], str]
    dangerous: bool = False
    # Reaches another person or another machine. Always confirmed, whatever the
    # approval policy says. See AGENT.md, "What it must never do without asking".
    outward: bool = False
    # The result carries content from outside - a file, a page, a command's
    # output. The loop wraps these so the model treats them as data.
    untrusted: bool = False

    def api_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            # Stream large inputs as they are generated rather than in one burst.
            "eager_input_streaming": True,
        }


def validate_input(schema: dict[str, Any], args: Any) -> str | None:
    """Check parsed tool input against its schema. Returns an error, or None.

    With eager input streaming the server stops validating, and the SDK's
    tolerant parser can hand back a truncated or mistyped object - so this runs
    before any handler does.
    """
    if not isinstance(args, dict):
        return f"expected an object of arguments, got {type(args).__name__}"
    properties: dict[str, Any] = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in args or args[name] is None:
            return f"missing required argument {name!r}"
    for name, value in args.items():
        spec = properties.get(name)
        if spec is None:
            if schema.get("additionalProperties") is False:
                return f"unexpected argument {name!r}"
            continue
        expected = spec.get("type")
        if expected and expected in _JSON_TYPES:
            # bool is an int subclass; keep the two apart.
            if expected in {"number", "integer"} and isinstance(value, bool):
                return f"{name!r} must be {expected}, got boolean"
            if not isinstance(value, _JSON_TYPES[expected]):
                return f"{name!r} must be {expected}, got {type(value).__name__}"
        choices = spec.get("enum")
        if choices and value not in choices:
            return f"{name!r} must be one of {choices}, got {value!r}"
    return None


def truncate(text: str, limit: int, note: str = "output") -> str:
    """Keep tool results from swallowing the context window."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[: limit - 200]
    return f"{head}\n\n[... {len(text) - limit + 200} more characters of {note} truncated ...]"


class ToolRegistry:
    """The set of tools available to one agent."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def subset(self, names: Iterable[str]) -> ToolRegistry:
        wanted = set(names)
        return ToolRegistry(tool for name, tool in self._tools.items() if name in wanted)

    def api_payload(self, server_tools: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
        """The `tools` parameter. Sorted, so the cached prefix stays stable."""
        payload = [self._tools[name].api_dict() for name in sorted(self._tools)]
        payload.extend(server_tools)
        return payload


@dataclass
class ToolResult:
    """What the loop sends back as a tool_result block."""

    content: str
    is_error: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
