"""Test fixtures: a fake Messages API so the suite never touches the network."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from jarvis.config import JarvisConfig
from jarvis.memory import Store
from jarvis.tasks import TaskRegistry
from jarvis.tools.base import ToolContext


# --- fake SDK objects ------------------------------------------------
@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    name: str
    input: Any
    id: str = "tu_1"
    type: str = "tool_use"


@dataclass
class Usage:
    input_tokens: int = 100
    output_tokens: int = 20
    cache_read_input_tokens: int = 0


@dataclass
class StopDetails:
    category: str = "cyber"
    explanation: str = "policy"


@dataclass
class FakeMessage:
    content: list[Any]
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)
    stop_details: StopDetails | None = None


@dataclass
class StreamTextEvent:
    text: str
    type: str = "text"


class FakeStream:
    """Mimics the context manager returned by client.messages.stream()."""

    def __init__(self, message: FakeMessage, raises: Exception | None = None) -> None:
        self._message = message
        self._raises = raises

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self):
        if self._raises is not None:
            raise self._raises
        for block in self._message.content:
            if getattr(block, "type", "") == "text":
                yield StreamTextEvent(text=block.text)

    def get_final_message(self) -> FakeMessage:
        return self._message


class FakeMessages:
    def __init__(self, turns: list[Any]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        if not self.turns:
            raise AssertionError("the agent asked for more turns than the test scripted")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            return FakeStream(FakeMessage(content=[]), raises=turn)
        return FakeStream(turn)


class FakeClient:
    """Stands in for anthropic.Anthropic. `beta.messages` shares the script."""

    def __init__(self, turns: list[Any]) -> None:
        self.messages = FakeMessages(turns)
        self.beta = type("Beta", (), {"messages": self.messages})()

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.messages.calls


# --- turn builders ---------------------------------------------------
def text_turn(text: str, stop_reason: str = "end_turn") -> FakeMessage:
    return FakeMessage(content=[TextBlock(text=text)], stop_reason=stop_reason)


def tool_turn(
    calls: list[tuple[str, Any]], text: str = "", stop_reason: str = "tool_use"
) -> FakeMessage:
    content: list[Any] = [TextBlock(text=text)] if text else []
    for index, (name, args) in enumerate(calls, start=1):
        content.append(ToolUseBlock(name=name, input=args, id=f"tu_{index}"))
    return FakeMessage(content=content, stop_reason=stop_reason)


# --- fixtures --------------------------------------------------------
@pytest.fixture
def config(tmp_path) -> JarvisConfig:
    return JarvisConfig(
        workspace=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        approval="auto",
        max_iterations=8,
    )


@pytest.fixture
def workspace(config) -> Any:
    config.workspace.mkdir(parents=True, exist_ok=True)
    return config.workspace


@pytest.fixture
def store() -> Store:
    store = Store(":memory:")
    yield store
    store.close()


@pytest.fixture
def context(config, workspace, store) -> ToolContext:
    return ToolContext(config=config, store=store, tasks=TaskRegistry(max_workers=2))
