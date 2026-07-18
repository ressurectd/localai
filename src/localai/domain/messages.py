"""Conversation primitives shared by every layer.

These types are provider-neutral on purpose: adding a second model provider means
writing a translation in that provider's module, not changing the domain. The UI,
the storage layer and the agent loop all speak in these objects.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class TokenSource(StrEnum):
    """Whether a token count came from the provider or was inferred by us.

    Every stored token figure carries one of these. The application never presents
    an estimate as though it were exact -- see docs/database-schema.md#usage_records.
    """

    REPORTED = "reported"
    """The provider returned an exact count."""

    ESTIMATED = "estimated"
    """Derived heuristically (roughly 4 characters per token). Displayed with '~'."""

    UNKNOWN = "unknown"
    """Neither reported nor derivable; displayed as '?' rather than zero."""


def new_id(prefix: str) -> str:
    """Short, sortable-enough identifier. Not a security token."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass(slots=True)
class ToolCall:
    """A model's request to invoke a tool.

    Ollama does not assign identifiers to tool calls, so we mint one on receipt.
    The identifier ties the request, the permission decision, the audit record and
    the resulting tool message together.
    """

    name: str
    arguments: dict[str, Any]
    id: str = field(default_factory=lambda: new_id("call"))

    @classmethod
    def from_ollama(cls, raw: dict[str, Any]) -> Self:
        """Parse one entry of ``message.tool_calls``, tolerating string arguments.

        Some models emit ``arguments`` as a JSON *string* rather than an object even
        when using native tool calling. Rejecting those outright would needlessly
        fail an otherwise well-formed call, so we parse defensively and let schema
        validation in the registry deliver the real verdict.
        """
        function = raw.get("function") or {}
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"__raw__": arguments}
        if not isinstance(arguments, dict):
            arguments = {"__raw__": arguments}
        return cls(name=str(function.get("name", "")), arguments=arguments)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(slots=True)
class Message:
    """One entry in a conversation."""

    role: Role
    content: str = ""
    thinking: str = ""
    """Reasoning text, when the model exposes it separately. Never sent back as context."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    """Set on TOOL messages to link the result to its request."""

    name: str | None = None
    """Tool name on TOOL messages."""

    created_at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: new_id("msg"))
    metadata: dict[str, Any] = field(default_factory=dict)
    """Non-model-visible annotations: injection flags, truncation notes, timings."""

    def to_ollama(self) -> dict[str, Any]:
        """Render for the ``/api/chat`` messages array.

        Thinking text is deliberately omitted: replaying a model's own reasoning back
        to it wastes context and degrades output on most local models.
        """
        payload: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [
                {"function": {"name": c.name, "arguments": c.arguments}} for c in self.tool_calls
            ]
        if self.role is Role.TOOL and self.name:
            payload["tool_name"] = self.name
        return payload

    def approx_tokens(self) -> int:
        """Rough token count for the context meter.

        Four characters per token is the usual English-text approximation. It is
        always surfaced as an estimate; exact prompt counts arrive from Ollama in
        the response and supersede it in the usage database.
        """
        chars = len(self.content) + sum(
            len(c.name) + len(json.dumps(c.arguments)) for c in self.tool_calls
        )
        return max(1, chars // 4) + 4  # +4 for role framing overhead


@dataclass(slots=True)
class Usage:
    """Token and timing accounting for a single generation.

    Ollama reports ``prompt_eval_count`` and ``eval_count`` exactly, so those are
    marked REPORTED. It does *not* separately report reasoning tokens -- they are
    counted inside ``eval_count`` -- so ``thinking_tokens`` is always an estimate
    and is marked as such rather than being invented or silently zeroed.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    thinking_tokens: int = 0
    token_source: TokenSource = TokenSource.UNKNOWN
    thinking_token_source: TokenSource = TokenSource.UNKNOWN
    total_duration_ns: int = 0
    load_duration_ns: int = 0
    prompt_eval_duration_ns: int = 0
    eval_duration_ns: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def tokens_per_second(self) -> float | None:
        """Generation throughput, or None when Ollama did not report a duration."""
        if self.eval_duration_ns <= 0 or self.completion_tokens <= 0:
            return None
        return self.completion_tokens / (self.eval_duration_ns / 1e9)

    @property
    def wall_seconds(self) -> float:
        return self.total_duration_ns / 1e9

    def energy_wh_estimate(self, assumed_watts: float) -> float:
        """Very rough energy estimate. Always labelled an estimate at every call site.

        This multiplies assumed whole-system draw by generation wall time. It ignores
        idle baseline, GPU/CPU split and power-supply efficiency, so it is useful only
        for relative comparison between sessions -- never as a real measurement.
        """
        return assumed_watts * (self.wall_seconds / 3600.0)

    @classmethod
    def from_ollama_final(cls, payload: dict[str, Any], thinking_text: str = "") -> Self:
        """Build from the terminal ``done: true`` chunk of a chat stream."""
        prompt = payload.get("prompt_eval_count")
        completion = payload.get("eval_count")
        reported = prompt is not None or completion is not None
        return cls(
            prompt_tokens=int(prompt or 0),
            completion_tokens=int(completion or 0),
            thinking_tokens=max(1, len(thinking_text) // 4) if thinking_text else 0,
            token_source=TokenSource.REPORTED if reported else TokenSource.UNKNOWN,
            thinking_token_source=(TokenSource.ESTIMATED if thinking_text else TokenSource.UNKNOWN),
            total_duration_ns=int(payload.get("total_duration") or 0),
            load_duration_ns=int(payload.get("load_duration") or 0),
            prompt_eval_duration_ns=int(payload.get("prompt_eval_duration") or 0),
            eval_duration_ns=int(payload.get("eval_duration") or 0),
        )
