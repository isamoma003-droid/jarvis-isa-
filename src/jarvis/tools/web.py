"""Web access.

Search and fetch run on Anthropic's servers - they are declared, not
implemented, and their results come back inside the same response. The agent
loop must not try to execute them locally.
"""

from __future__ import annotations

from typing import Any

# Dynamic-filtering variants; supported on Opus 5. Code execution runs under the
# hood for these, so do not declare a code_execution tool alongside them.
WEB_SEARCH = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 8,
}

WEB_FETCH = {
    "type": "web_fetch_20260209",
    "name": "web_fetch",
    "max_uses": 8,
    "max_content_tokens": 30000,
    "citations": {"enabled": True},
}

SERVER_TOOL_NAMES = frozenset({"web_search", "web_fetch"})


def server_tools(enabled: bool = True) -> list[dict[str, Any]]:
    return [WEB_SEARCH, WEB_FETCH] if enabled else []
