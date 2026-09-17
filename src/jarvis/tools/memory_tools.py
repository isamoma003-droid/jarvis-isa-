"""Memory tools: what Jarvis keeps about you between sessions."""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext, truncate


def remember(ctx: ToolContext, args: dict[str, Any]) -> str:
    fact = ctx.store.remember(
        key=args["key"],
        value=args["value"],
        category=args.get("category", "general"),
    )
    return f"Remembered {fact.key!r} = {fact.value!r} (category: {fact.category})"


def recall(ctx: ToolContext, args: dict[str, Any]) -> str:
    facts = ctx.store.recall(args.get("query", ""), limit=int(args.get("limit") or 50))
    if not facts:
        return "Nothing remembered that matches."
    body = "\n".join(f"- {f.key} [{f.category}]: {f.value}" for f in facts)
    return truncate(body, ctx.config.max_tool_output, "facts")


def forget(ctx: ToolContext, args: dict[str, Any]) -> str:
    key = args["key"]
    return f"Forgot {key!r}." if ctx.store.forget(key) else f"Nothing stored under {key!r}."


TOOLS = [
    Tool(
        name="remember",
        description=(
            "Store a durable fact about the user or their setup, so it survives restarts. "
            "Use a short stable key ('name', 'timezone', 'work.repo'). Storing an existing "
            "key replaces it. Do not store secrets or passwords."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short stable identifier."},
                "value": {"type": "string"},
                "category": {
                    "type": "string",
                    "description": "e.g. identity, preference, project.",
                },
            },
            "required": ["key", "value"],
        },
        handler=remember,
    ),
    Tool(
        name="recall",
        description=(
            "Look up remembered facts. Omit the query to list everything. "
            "Facts already in your system prompt do not need a lookup."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Substring to match."},
                "limit": {"type": "integer"},
            },
        },
        handler=recall,
    ),
    Tool(
        name="forget",
        description="Delete a remembered fact by its key.",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
        handler=forget,
    ),
]
