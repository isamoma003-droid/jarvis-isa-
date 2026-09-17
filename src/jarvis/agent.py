"""The agent loop: stream a turn, run the tools it asks for, repeat.

One implementation serves every interface. `run()` is a generator of events so
the terminal, the web socket, and the voice loop all consume the same stream
and none of them needs to know how a turn is driven.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import anthropic

from .config import JarvisConfig
from .errors import ToolError
from .events import (
    ErrorEvent,
    Event,
    Notice,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from .tools.base import ToolContext, ToolRegistry, truncate, validate_input
from .tools.web import server_tools

# The beta flag that pairs with the scalar `fallbacks: "default"` form.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_JSON_RETRIES = 2


class Agent:
    """Drives one conversation against the Messages API."""

    def __init__(
        self,
        *,
        client: Any,
        config: JarvisConfig,
        registry: ToolRegistry,
        context: ToolContext,
        system: list[dict[str, Any]] | str,
        model: str | None = None,
        effort: str | None = None,
        name: str = "jarvis",
        server_tools_enabled: bool = True,
        max_iterations: int | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.registry = registry
        self.context = context
        self.system = system
        self.model = model or config.model
        self.effort = effort or config.effort
        self.name = name
        self.max_iterations = max_iterations or config.max_iterations
        self._tools = registry.api_payload(
            server_tools(server_tools_enabled and config.web_tools)
        )
        self._cancel = threading.Event()

    # -- control -------------------------------------------------------
    def cancel(self) -> None:
        """Ask the current turn to stop at the next safe point."""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- request shaping -----------------------------------------------
    def _request_kwargs(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.config.max_tokens,
            "system": self.system,
            "messages": messages,
            "output_config": {"effort": self.effort},
        }
        if self._tools:
            kwargs["tools"] = self._tools
        if self.config.thinking:
            kwargs["thinking"] = {
                "type": "adaptive",
                "display": self.config.thinking_display,
            }
        return kwargs

    def _open_stream(self, messages: list[dict[str, Any]]):
        kwargs = self._request_kwargs(messages)
        if self.config.refusal_fallbacks:
            # On a policy decline the API re-runs the turn on a fallback model
            # inside the same call instead of simply stopping.
            return self.client.beta.messages.stream(
                betas=[FALLBACK_BETA], fallbacks="default", **kwargs
            )
        return self.client.messages.stream(**kwargs)

    # -- tool execution ------------------------------------------------
    def _execute(self, block: Any) -> tuple[dict[str, Any], bool]:
        """Run one tool call. Returns the tool_result block and whether it failed."""
        tool = self.registry.get(block.name)

        if tool is None:
            return self._result_block(
                block,
                f"unknown tool {block.name!r}; available: {', '.join(self.registry.names())}",
                is_error=True,
            )

        problem = validate_input(tool.input_schema, block.input)
        if problem is not None:
            # Eager input streaming means the server did not validate this and
            # the SDK's parser is tolerant, so a truncated or mistyped object
            # reaches us intact. Reject it instead of running it.
            return self._result_block(
                block,
                f"INVALID_JSON: {problem}. Re-send this tool call with arguments "
                f"matching the schema for {tool.name}.",
                is_error=True,
            )

        try:
            output = tool.handler(self.context, dict(block.input))
        except ToolError as exc:
            return self._result_block(block, str(exc), is_error=True)
        except (OSError, ValueError, KeyError) as exc:
            return self._result_block(block, f"{type(exc).__name__}: {exc}", is_error=True)
        except Exception as exc:  # a tool bug must not end the conversation
            return self._result_block(
                block, f"tool {tool.name} failed unexpectedly: {exc!r}", is_error=True
            )
        return self._result_block(block, output, is_error=False)

    def _result_block(
        self, block: Any, output: str, *, is_error: bool
    ) -> tuple[dict[str, Any], bool]:
        text = truncate(str(output), self.config.max_tool_output, "tool output") or "(no output)"
        return (
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": text,
                "is_error": is_error,
            },
            is_error,
        )

    # -- the loop ------------------------------------------------------
    def run(self, messages: list[dict[str, Any]]) -> Iterator[Event]:
        """Run a turn to completion, yielding events. `messages` is extended."""
        self._cancel.clear()
        final_text = ""
        usage = {"input": 0, "output": 0, "cache_read": 0}
        json_retries = 0
        iterations = 0
        stop_reason: str | None = None

        while True:
            if self._cancel.is_set():
                yield Notice(message="stopped", level="warn")
                break

            iterations += 1
            if iterations > self.max_iterations:
                yield Notice(
                    message=f"stopped after {self.max_iterations} steps without finishing",
                    level="warn",
                )
                break

            try:
                with self._open_stream(messages) as stream:
                    for event in stream:
                        if self._cancel.is_set():
                            break
                        kind = getattr(event, "type", "")
                        if kind == "text":
                            yield TextDelta(text=event.text)
                        elif kind == "thinking":
                            thought = getattr(event, "thinking", "")
                            if thought:
                                yield ThinkingDelta(text=thought)
                    response = stream.get_final_message()
                json_retries = 0
            except ValueError:
                # Tool-call JSON the SDK could not parse at all. It raised before
                # the block completed, so there is no tool_use_id to answer:
                # re-issue the turn, but not forever.
                json_retries += 1
                if json_retries > MAX_JSON_RETRIES:
                    yield ErrorEvent(
                        message="the model kept producing unparseable tool input",
                        recoverable=False,
                    )
                    break
                yield Notice(message="unreadable tool input, retrying", level="warn")
                continue
            except anthropic.NotFoundError as exc:
                yield ErrorEvent(message=f"model or endpoint not found: {exc}", recoverable=False)
                break
            except anthropic.RateLimitError as exc:
                retry_after = exc.response.headers.get("retry-after", "a moment")
                yield ErrorEvent(message=f"rate limited; retry after {retry_after}")
                break
            except anthropic.APIStatusError as exc:
                yield ErrorEvent(
                    message=f"API error {exc.status_code}: {exc.message}",
                    recoverable=exc.status_code >= 500,
                )
                break
            except anthropic.APIConnectionError as exc:
                yield ErrorEvent(message=f"could not reach the API: {exc}")
                break

            usage["input"] += getattr(response.usage, "input_tokens", 0) or 0
            usage["output"] += getattr(response.usage, "output_tokens", 0) or 0
            usage["cache_read"] += getattr(response.usage, "cache_read_input_tokens", 0) or 0
            stop_reason = response.stop_reason

            text_now = "".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
            if text_now:
                final_text = text_now

            if stop_reason == "pause_turn":
                # A server-side tool hit its iteration limit mid-turn; hand the
                # paused turn straight back to continue it.
                messages.append({"role": "assistant", "content": response.content})
                continue

            tool_uses = [block for block in response.content if block.type == "tool_use"]

            if stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                reason = getattr(details, "explanation", None) or "no explanation given"
                yield Notice(message=f"the model declined this request ({reason})", level="warn")
                messages.append({"role": "assistant", "content": response.content})
                break

            if not tool_uses:
                messages.append({"role": "assistant", "content": response.content})
                break

            if stop_reason == "max_tokens":
                # A truncated tool input parses as a valid partial object. Do not run it.
                yield ErrorEvent(
                    message="the response was cut off mid tool call; raise max_tokens",
                    recoverable=False,
                )
                break

            messages.append({"role": "assistant", "content": response.content})

            results = []
            for block in tool_uses:
                yield ToolStarted(
                    name=block.name,
                    tool_use_id=block.id,
                    input=dict(block.input) if isinstance(block.input, dict) else {},
                )
                started = time.monotonic()
                result, failed = self._execute(block)
                results.append(result)
                yield ToolFinished(
                    name=block.name,
                    tool_use_id=block.id,
                    result=result["content"],
                    is_error=failed,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                yield from self.context.drain()

            # Every result goes back in one user message: splitting them teaches
            # the model to stop calling tools in parallel.
            messages.append({"role": "user", "content": results})

        yield TurnFinished(
            text=final_text,
            stop_reason=stop_reason,
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            cache_read_tokens=usage["cache_read"],
        )

    # -- convenience ---------------------------------------------------
    def run_to_text(self, prompt: str) -> str:
        """Run one turn and return the final answer. Used by sub-agents."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        answer = ""
        failure = ""
        for event in self.run(messages):
            if isinstance(event, TurnFinished):
                answer = event.text
            elif isinstance(event, ErrorEvent):
                failure = event.message
        if failure and not answer:
            raise ToolError(f"sub-agent '{self.name}' failed: {failure}")
        return answer or "(the sub-agent finished without producing a report)"


def describe_tool_input(args: dict[str, Any], limit: int = 120) -> str:
    """A one-line rendering of tool arguments, for logs and the terminal."""
    if not args:
        return ""
    parts = []
    for key, value in args.items():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        text = " ".join(str(text).split())
        parts.append(f"{key}={text[:limit]}" if len(text) > limit else f"{key}={text}")
    return " ".join(parts)[: limit * 2]
