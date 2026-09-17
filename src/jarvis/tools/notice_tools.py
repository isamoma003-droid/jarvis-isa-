"""Notice tools: the inbox the heartbeat fills, and clearing it.

Anything Jarvis surfaces on its own has to be something the user can acknowledge
and clear, or the inbox becomes clutter they start ignoring - which is the same
as having no inbox at all.
"""

from __future__ import annotations

from typing import Any

from ..errors import ToolError
from ..memory import NOTICE_LEVELS
from .base import Tool, ToolContext, truncate

_LOCAL_LIMIT = 40


def _line(notice: Any) -> str:
    mark = {"urgent": "!", "notify": "*"}.get(notice.level, "-")
    seen = "" if notice.status == "pending" else " (seen)"
    again = f" (seen {notice.repeats + 1}x)" if notice.repeats else ""
    return f"{mark} #{notice.id} [{notice.source}]{seen} {notice.text}{again}"


def list_notices(ctx: ToolContext, args: dict[str, Any]) -> str:
    limit = int(args.get("limit") or _LOCAL_LIMIT)
    notices = ctx.store.open_notices(limit=limit)
    if not notices:
        return "The inbox is empty - nothing waiting."
    body = "\n".join(_line(notice) for notice in notices)
    # Listing is seeing. Seeing is not clearing: they stay open until dismissed.
    ctx.store.mark_delivered([n.id for n in notices if n.status == "pending"])
    return truncate(
        f"{len(notices)} open:\n{body}\n\nDismiss one with dismiss_notice.",
        ctx.config.max_tool_output,
        "notices",
    )


def dismiss_notice(ctx: ToolContext, args: dict[str, Any]) -> str:
    if args.get("all"):
        cleared = ctx.store.dismiss_all_notices()
        return f"Dismissed {cleared} notice(s); the inbox is empty." if cleared else "Nothing open."
    if "id" not in args:
        raise ToolError("give a notice id, or set all=true to clear the inbox")
    notice_id = int(args["id"])
    if ctx.store.dismiss_notice(notice_id):
        return f"Dismissed notice #{notice_id}."
    return f"Notice #{notice_id} is not open - already dismissed, or never existed."


def surface(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Let the agent put something in the inbox itself, for later."""
    level = args.get("level", "quiet")
    if level not in NOTICE_LEVELS:
        raise ToolError(f"level must be one of {NOTICE_LEVELS}")
    notice = ctx.store.add_notice(
        source="jarvis", text=args["text"], detail=args.get("detail", ""), level=level
    )
    return f"Noted as #{notice.id} ({level}); it will be waiting in the inbox."


TOOLS = [
    Tool(
        name="list_notices",
        description=(
            "List what Jarvis has surfaced on its own and the user has not cleared yet - "
            "results from scheduled checks, reminders that fired while they were away. "
            "Use this when they ask what they missed, what is waiting, or what you noticed."
        ),
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "How many, newest last."}},
        },
        handler=list_notices,
    ),
    Tool(
        name="dismiss_notice",
        description=(
            "Clear a notice from the inbox once the user has dealt with it, by id. "
            "Set all=true to clear everything open. Only do this when they say so."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "The notice id, as listed."},
                "all": {"type": "boolean", "description": "Clear the whole inbox."},
            },
        },
        handler=dismiss_notice,
    ),
    Tool(
        name="surface",
        description=(
            "Put something in the user's inbox for later, instead of interrupting now. "
            "Use 'quiet' for anything that can wait (the default), 'notify' for something "
            "they should see today, and 'urgent' only for what justifies waking them - "
            "urgent ignores quiet hours, so be sure it deserves that."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "One line: what they need to know."},
                "detail": {"type": "string", "description": "The longer version, if any."},
                "level": {"type": "string", "enum": list(NOTICE_LEVELS)},
            },
            "required": ["text"],
        },
        handler=surface,
    ),
]
