"""The agent loop.

One turn is: send context to the model, stream its response, execute any tool calls
it requests, feed the results back, and repeat until the model answers in plain text
or a guard rail fires.

The loop emits :class:`AgentEvent` objects rather than writing to a display. The TUI
renders them, the CLI prints them and tests assert on them -- so the loop contains
no presentation logic, and the UI contains no agent logic.

Guard rails, all configurable and all tested:

* ``max_tool_iterations`` -- a hard cap on tool rounds per turn.
* ``max_turn_seconds``    -- wall-clock budget for the whole turn.
* ``max_identical_calls`` -- catches a model stuck re-reading the same file.
* cancellation           -- an ``asyncio.Event`` checked between every stage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from localai.agent import fallback
from localai.domain.messages import Message, Role, ToolCall, Usage
from localai.errors import ProviderError
from localai.permissions.engine import Caller
from localai.providers.base import ModelInfo, ModelProvider
from localai.tools.base import Tool, ToolContext, ToolResult
from localai.tools.runner import ToolRunner

log = logging.getLogger(__name__)


class EventKind(StrEnum):
    """Every event the loop can emit. The UI switches on this."""

    TURN_START = "turn_start"
    THINKING_DELTA = "thinking_delta"
    CONTENT_DELTA = "content_delta"
    MESSAGE_COMPLETE = "message_complete"
    TOOL_REQUESTED = "tool_requested"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    TURN_COMPLETE = "turn_complete"
    ERROR = "error"
    CANCELLED = "cancelled"
    GUARD_TRIPPED = "guard_tripped"


@dataclass(slots=True)
class AgentEvent:
    """A single occurrence during a turn."""

    kind: EventKind
    text: str = ""
    message: Message | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    usage: Usage | None = None
    iteration: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TurnResult:
    """Summary of a completed turn, for the caller to persist."""

    messages: list[Message] = field(default_factory=list)
    usages: list[Usage] = field(default_factory=list)
    tool_calls: int = 0
    iterations: int = 0
    cancelled: bool = False
    error: str | None = None
    duration_s: float = 0.0

    @property
    def final_text(self) -> str:
        for message in reversed(self.messages):
            if message.role is Role.ASSISTANT and message.content:
                return message.content
        return ""

    def combined_usage(self) -> Usage:
        """Sum usage across every generation in the turn, preserving provenance.

        The combined ``token_source`` is the *weakest* of the individual sources: if
        any generation lacked reported counts, the total cannot honestly be called
        exact.
        """
        total = Usage()
        for usage in self.usages:
            total.prompt_tokens += usage.prompt_tokens
            total.completion_tokens += usage.completion_tokens
            total.thinking_tokens += usage.thinking_tokens
            total.total_duration_ns += usage.total_duration_ns
            total.load_duration_ns += usage.load_duration_ns
            total.prompt_eval_duration_ns += usage.prompt_eval_duration_ns
            total.eval_duration_ns += usage.eval_duration_ns
        if self.usages:
            total.token_source = min(
                (u.token_source for u in self.usages), key=_SOURCE_RANK.__getitem__
            )
            total.thinking_token_source = min(
                (u.thinking_token_source for u in self.usages), key=_SOURCE_RANK.__getitem__
            )
        return total


#: Lower is weaker. Used to pick the least confident source when combining usage.
_SOURCE_RANK = {"unknown": 0, "estimated": 1, "reported": 2}


class AgentLoop:
    """Drives a conversation turn to completion."""

    def __init__(
        self,
        provider: ModelProvider,
        runner: ToolRunner,
        *,
        caller: Caller | None = None,
    ) -> None:
        self.provider = provider
        self.runner = runner
        self.caller = caller or Caller()

    async def run_turn(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[Tool],
        context: ToolContext,
        model_info: ModelInfo | None = None,
        options: dict[str, Any] | None = None,
        think: bool | str | None = None,
        keep_alive: str | None = None,
        images: Sequence[bytes] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one turn, yielding events as they occur.

        ``messages`` is the full context to send. The caller owns history; the loop
        appends to a local working copy and reports the new messages in the final
        :class:`TurnResult` so the caller can persist them.
        """
        config = context.config.agent
        started = time.monotonic()
        deadline = started + config.max_turn_seconds

        native_tools = model_info is None or model_info.supports_tools
        use_fallback = bool(tools) and not native_tools and config.structured_fallback

        working: list[Message] = list(messages)
        result = TurnResult()
        call_fingerprints: Counter[str] = Counter()

        if use_fallback:
            working = _inject_fallback_instructions(working, list(tools))

        yield AgentEvent(
            EventKind.TURN_START,
            detail={
                "model": model,
                "tools": len(tools),
                "native_tool_calling": native_tools,
                "fallback_mode": use_fallback,
            },
        )

        for iteration in range(1, config.max_tool_iterations + 1):
            result.iterations = iteration

            if context.cancelled():
                result.cancelled = True
                yield AgentEvent(EventKind.CANCELLED, text="Cancelled before generation.")
                break
            if time.monotonic() > deadline:
                result.error = "turn time limit reached"
                yield AgentEvent(
                    EventKind.GUARD_TRIPPED,
                    text=(
                        f"This turn exceeded its {config.max_turn_seconds:.0f}s budget after "
                        f"{iteration - 1} tool round(s) and was stopped."
                    ),
                    detail={"guard": "max_turn_seconds"},
                )
                break

            # -- generate ------------------------------------------------------
            assistant = Message(role=Role.ASSISTANT)
            content_parts: list[str] = []
            thinking_parts: list[str] = []
            streamed_calls: list[ToolCall] = []

            try:
                async for chunk in self.provider.chat(
                    model,
                    working,
                    tools=[t.to_ollama_schema() for t in tools]
                    if (tools and native_tools)
                    else None,
                    options=options,
                    think=think,
                    keep_alive=keep_alive,
                    images=images if iteration == 1 else None,
                ):
                    if context.cancelled():
                        result.cancelled = True
                        break

                    if chunk.thinking:
                        thinking_parts.append(chunk.thinking)
                        yield AgentEvent(
                            EventKind.THINKING_DELTA, text=chunk.thinking, iteration=iteration
                        )
                    if chunk.content:
                        content_parts.append(chunk.content)
                        yield AgentEvent(
                            EventKind.CONTENT_DELTA, text=chunk.content, iteration=iteration
                        )
                    if chunk.tool_calls:
                        streamed_calls.extend(chunk.tool_calls)
                    if chunk.done and chunk.usage:
                        result.usages.append(chunk.usage)
                        yield AgentEvent(EventKind.USAGE, usage=chunk.usage, iteration=iteration)

            except asyncio.CancelledError:
                result.cancelled = True
                yield AgentEvent(EventKind.CANCELLED, text="Generation cancelled.")
                break
            except ProviderError as exc:
                result.error = exc.message
                yield AgentEvent(
                    EventKind.ERROR,
                    text=exc.message,
                    detail={"remediation": exc.remediation, "code": exc.code},
                )
                break

            assistant.content = "".join(content_parts)
            assistant.thinking = "".join(thinking_parts)

            if result.cancelled:
                if assistant.content or assistant.thinking:
                    # Keep the partial response: the user asked to stop, not to discard.
                    assistant.metadata["cancelled"] = True
                    working.append(assistant)
                    result.messages.append(assistant)
                    yield AgentEvent(EventKind.MESSAGE_COMPLETE, message=assistant)
                yield AgentEvent(EventKind.CANCELLED, text="Cancelled.")
                break

            # -- collect tool calls --------------------------------------------
            calls = list(streamed_calls)
            if use_fallback and not calls:
                parsed, remaining = fallback.parse_tool_calls(assistant.content)
                if parsed:
                    calls = parsed
                    assistant.content = remaining
                    assistant.metadata["fallback_tool_call"] = True

            assistant.tool_calls = calls
            working.append(assistant)
            result.messages.append(assistant)
            yield AgentEvent(EventKind.MESSAGE_COMPLETE, message=assistant, iteration=iteration)

            if not calls:
                break  # plain-text answer: the turn is done

            # -- repetition guard ------------------------------------------------
            tripped = False
            for call in calls:
                fingerprint = _fingerprint(call)
                call_fingerprints[fingerprint] += 1
                if call_fingerprints[fingerprint] > config.max_identical_calls:
                    result.error = "repeated identical tool call"
                    yield AgentEvent(
                        EventKind.GUARD_TRIPPED,
                        text=(
                            f"The model called {call.name} with identical arguments "
                            f"{call_fingerprints[fingerprint]} times. Stopping to avoid a loop."
                        ),
                        detail={"guard": "max_identical_calls", "tool": call.name},
                    )
                    tripped = True
                    break
            if tripped:
                break

            # -- execute tools ----------------------------------------------------
            for call in calls:
                if context.cancelled():
                    result.cancelled = True
                    break

                yield AgentEvent(
                    EventKind.TOOL_REQUESTED,
                    tool_call=call,
                    iteration=iteration,
                    text=self._preview(call),
                )

                try:
                    tool_result = await self.runner.execute(
                        call.name,
                        call.arguments,
                        context,
                        caller=self.caller,
                        call_id=call.id,
                    )
                except asyncio.CancelledError:
                    result.cancelled = True
                    yield AgentEvent(EventKind.CANCELLED, text="Tool execution cancelled.")
                    break

                result.tool_calls += 1
                yield AgentEvent(
                    EventKind.TOOL_RESULT,
                    tool_call=call,
                    tool_result=tool_result,
                    iteration=iteration,
                )

                observation = tool_result.content or (tool_result.error or "(no output)")
                if use_fallback:
                    # No `tool` role available: deliver the result as a user turn.
                    message = Message(
                        role=Role.USER,
                        content=fallback.format_tool_result(call.name, observation),
                        metadata={"synthetic_tool_result": True, "tool": call.name},
                    )
                else:
                    message = Message(
                        role=Role.TOOL,
                        content=observation,
                        tool_call_id=call.id,
                        name=call.name,
                        metadata={"flags": tool_result.flags, "ok": tool_result.ok},
                    )
                working.append(message)
                result.messages.append(message)

            if result.cancelled:
                break
        else:
            # The for-loop completed without break: the iteration cap was reached.
            result.error = "tool iteration limit reached"
            yield AgentEvent(
                EventKind.GUARD_TRIPPED,
                text=(
                    f"Reached the limit of {config.max_tool_iterations} tool rounds in one turn. "
                    "Ask a narrower question, or raise agent.max_tool_iterations."
                ),
                detail={"guard": "max_tool_iterations"},
            )

        result.duration_s = time.monotonic() - started
        yield AgentEvent(
            EventKind.TURN_COMPLETE,
            detail={
                "iterations": result.iterations,
                "tool_calls": result.tool_calls,
                "cancelled": result.cancelled,
                "error": result.error,
                "duration_s": round(result.duration_s, 2),
            },
        )
        # Attach the result to the terminal event so callers need not thread it out
        # of the generator separately.
        self.last_result = result

    def _preview(self, call: ToolCall) -> str:
        """Human-readable description of a requested call, shown before execution."""
        try:
            return self.runner.registry.get(call.name).describe_call(call.arguments)
        except Exception:
            return f"{call.name}({json.dumps(call.arguments, default=str)[:120]})"


def _fingerprint(call: ToolCall) -> str:
    """Stable hash of a tool call, for the repetition guard."""
    payload = json.dumps(
        {"tool": call.name, "arguments": call.arguments}, sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _inject_fallback_instructions(messages: list[Message], tools: list[Tool]) -> list[Message]:
    """Append the fallback protocol to the system message, or add one.

    Appending rather than prepending keeps the user's own system prompt in the
    dominant leading position, which matters on small models.
    """
    instructions = fallback.build_instructions(tools)
    if not instructions:
        return messages

    out = list(messages)
    for index, message in enumerate(out):
        if message.role is Role.SYSTEM:
            out[index] = Message(
                role=Role.SYSTEM,
                content=f"{message.content}\n\n{instructions}".strip(),
                metadata={**message.metadata, "fallback_instructions": True},
            )
            return out
    return [Message(role=Role.SYSTEM, content=instructions), *out]
