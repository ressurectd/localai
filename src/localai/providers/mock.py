"""Deterministic in-process provider for tests, CI and ``localai dev sandbox``.

This exists so that no test ever depends on the user's real Ollama daemon, real
models or real conversations. It is scripted rather than random: you queue the
responses you want and assert on what the agent loop did with them.

Example::

    provider = MockProvider()
    provider.queue_tool_call("read_file", {"path": "notes.txt"})
    provider.queue_text("The file says hello.")
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from localai.domain.messages import Message, TokenSource, ToolCall, Usage
from localai.errors import ModelNotFoundError
from localai.providers.base import ChatChunk, ModelInfo

DEFAULT_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(
        name="mock-tools:8b",
        size_bytes=5_000_000_000,
        family="mock",
        parameter_size="8.0B",
        quantization="Q4_K_M",
        context_length=32768,
        capabilities=frozenset({"completion", "tools", "thinking"}),
        modified_at="2026-01-01T00:00:00Z",
    ),
    ModelInfo(
        name="mock-plain:3b",
        size_bytes=2_000_000_000,
        family="mock",
        parameter_size="3.0B",
        quantization="Q4_0",
        context_length=8192,
        capabilities=frozenset({"completion"}),  # no native tool calling: exercises fallback
        modified_at="2026-01-01T00:00:00Z",
    ),
    ModelInfo(
        name="mock-vision:7b",
        size_bytes=4_500_000_000,
        family="mock",
        parameter_size="7.0B",
        quantization="Q4_K_M",
        context_length=16384,
        capabilities=frozenset({"completion", "vision"}),
        modified_at="2026-01-01T00:00:00Z",
    ),
)


@dataclass(slots=True)
class ScriptedResponse:
    """One queued model turn."""

    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    chunk_size: int = 12
    """Text is emitted in slices of this size so streaming behaviour is exercised."""

    prompt_tokens: int = 42
    completion_tokens: int = 0
    report_tokens: bool = True
    """Set False to simulate a daemon that omits counts, exercising estimated paths."""


class MockProvider:
    """A scripted :class:`~localai.providers.base.ModelProvider`."""

    name = "mock"

    def __init__(self, models: Sequence[ModelInfo] = DEFAULT_MODELS) -> None:
        self._models = list(models)
        self._queue: list[ScriptedResponse] = []
        self._loaded: set[str] = set()
        #: Every chat() invocation, for assertions about what the loop actually sent.
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    # -- scripting ------------------------------------------------------------

    def queue(self, response: ScriptedResponse) -> MockProvider:
        self._queue.append(response)
        return self

    def queue_text(self, text: str, *, thinking: str = "", **kw: Any) -> MockProvider:
        return self.queue(ScriptedResponse(text=text, thinking=thinking, **kw))

    def queue_tool_call(self, name: str, arguments: dict[str, Any], **kw: Any) -> MockProvider:
        return self.queue(
            ScriptedResponse(tool_calls=[ToolCall(name=name, arguments=arguments)], **kw)
        )

    # -- provider protocol ----------------------------------------------------

    async def health(self) -> tuple[bool, str]:
        return True, "mock provider"

    async def version(self) -> str:
        return "mock-0"

    async def list_models(self) -> list[ModelInfo]:
        from dataclasses import replace

        return [replace(m, loaded=m.name in self._loaded) for m in self._models]

    async def show_model(self, name: str) -> ModelInfo:
        for model in await self.list_models():
            if model.name == name:
                return model
        raise ModelNotFoundError(
            f"model {name!r} is not installed",
            remediation=f"Available mock models: {', '.join(m.name for m in self._models)}",
            model=name,
        )

    async def loaded_models(self) -> list[ModelInfo]:
        return [m for m in await self.list_models() if m.loaded]

    async def chat(
        self,
        model: str,
        messages: Sequence[Message],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
        think: bool | str | None = None,
        keep_alive: str | None = None,
        images: Sequence[bytes] | None = None,
    ) -> AsyncIterator[ChatChunk]:
        await self.show_model(model)  # raise ModelNotFoundError for unknown models
        self.calls.append(
            {
                "model": model,
                "messages": [m.to_ollama() for m in messages],
                "tools": list(tools or []),
                "options": dict(options or {}),
                "think": think,
            }
        )

        response = (
            self._queue.pop(0)
            if self._queue
            else ScriptedResponse(text="(mock provider: nothing queued)")
        )

        if response.thinking:
            for i in range(0, len(response.thinking), response.chunk_size):
                yield ChatChunk(
                    thinking=response.thinking[i : i + response.chunk_size], model=model
                )

        for i in range(0, len(response.text), response.chunk_size):
            yield ChatChunk(content=response.text[i : i + response.chunk_size], model=model)

        if response.tool_calls:
            yield ChatChunk(tool_calls=list(response.tool_calls), model=model)

        completion = response.completion_tokens or max(1, len(response.text) // 4)
        yield ChatChunk(
            done=True,
            model=model,
            usage=Usage(
                prompt_tokens=response.prompt_tokens if response.report_tokens else 0,
                completion_tokens=completion if response.report_tokens else 0,
                thinking_tokens=len(response.thinking) // 4 if response.thinking else 0,
                token_source=(
                    TokenSource.REPORTED if response.report_tokens else TokenSource.UNKNOWN
                ),
                thinking_token_source=(
                    TokenSource.ESTIMATED if response.thinking else TokenSource.UNKNOWN
                ),
                total_duration_ns=1_000_000_000,
                eval_duration_ns=500_000_000,
            ),
        )

    async def preload(self, model: str, keep_alive: str = "-1") -> None:
        await self.show_model(model)
        self._loaded.add(model)

    async def unload(self, model: str) -> None:
        self._loaded.discard(model)

    async def aclose(self) -> None:
        self.closed = True
