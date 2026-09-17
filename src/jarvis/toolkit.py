"""Assembling the tool set an agent gets."""

from __future__ import annotations

from collections.abc import Iterable

from .config import JarvisConfig
from .tools import files, memory_tools, reminder_tools, shell, subagents
from .tools.base import ToolRegistry

ALL_TOOLS = [
    *files.TOOLS,
    *shell.TOOLS,
    *memory_tools.TOOLS,
    *reminder_tools.TOOLS,
    *subagents.TOOLS,
]

DELEGATION_TOOLS = {"delegate", "check_task", "list_tasks"}


def build_registry(
    config: JarvisConfig,
    only: Iterable[str] | None = None,
    delegation: bool = True,
) -> ToolRegistry:
    """Build a registry. `only` narrows it; delegation can be switched off."""
    wanted = set(only) if only is not None else None
    registry = ToolRegistry()
    for tool in ALL_TOOLS:
        if tool.name in DELEGATION_TOOLS:
            # Sub-agents only get delegation if they were explicitly given it.
            if not delegation or (wanted is not None and tool.name not in wanted):
                continue
            if wanted is None and config.max_depth < 1:
                continue
        elif wanted is not None and tool.name not in wanted:
            continue
        registry.register(tool)
    return registry
