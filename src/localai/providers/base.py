"""The model-provider contract.

``ModelProvider`` is a ``Protocol`` rather than an abstract base class so that test
doubles need only structural conformance. Two implementations ship: :mod:`ollama`
and :mod:`mock`. See docs/architecture.md#adding-a-model-provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from localai.domain.messages import Message, ToolCall, Usage


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Everything the UI needs to describe an installed model."""

    name: str
    size_bytes: int = 0
    family: str = ""
    parameter_size: str = ""
    quantization: str = ""
    context_length: int = 0
    embedding_length: int = 0
    capabilities: frozenset[str] = frozenset()
    modified_at: str = ""
    loaded: bool = False
    vram_bytes: int = 0
    """Resident size while loaded, as reported by ``/api/ps``. Zero when unloaded."""

    expires_at: str = ""

    @property
    def supports_tools(self) -> bool:
        return "tools" in self.capabilities

    @property
    def supports_thinking(self) -> bool:
        return "thinking" in self.capabilities

    @property
    def supports_vision(self) -> bool:
        return "vision" in self.capabilities

    @property
    def supports_embedding(self) -> bool:
        return "embedding" in self.capabilities

    def estimated_memory_bytes(self) -> int:
        """Approximate RAM/VRAM needed to serve this model.

        When loaded, Ollama tells us the true resident size and we use it. Otherwise
        we approximate as the on-disk weights plus a KV-cache allowance of roughly
        20%. Callers must present this as an estimate.
        """
        if self.vram_bytes:
            return self.vram_bytes
        return int(self.size_bytes * 1.2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size_bytes": self.size_bytes,
            "family": self.family,
            "parameter_size": self.parameter_size,
            "quantization": self.quantization,
            "context_length": self.context_length,
            "capabilities": sorted(self.capabilities),
            "supports_tools": self.supports_tools,
            "supports_thinking": self.supports_thinking,
            "supports_vision": self.supports_vision,
            "loaded": self.loaded,
            "vram_bytes": self.vram_bytes,
            "estimated_memory_bytes": self.estimated_memory_bytes(),
            "estimated_memory_is_estimate": self.vram_bytes == 0,
            "modified_at": self.modified_at,
        }


@dataclass(slots=True)
class ChatChunk:
    """One increment of a streamed response.

    Exactly one of the delta fields is populated on any given chunk, except the final
    chunk which carries ``done=True`` plus ``usage``.
    """

    content: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    done: bool = False
    usage: Usage | None = None
    model: str = ""


@runtime_checkable
class ModelProvider(Protocol):
    """A source of local language models."""

    name: str

    async def health(self) -> tuple[bool, str]:
        """Return ``(reachable, detail)``. Must never raise."""
        ...

    async def version(self) -> str:
        """Provider daemon version, or an empty string when unknown."""
        ...

    async def list_models(self) -> list[ModelInfo]:
        """All installed models, annotated with live load state where available."""
        ...

    async def show_model(self, name: str) -> ModelInfo:
        """Detailed information for one model. Raises ModelNotFoundError if absent."""
        ...

    async def loaded_models(self) -> list[ModelInfo]:
        """Models currently resident in memory."""
        ...

    def chat(
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
        """Stream a chat completion. Cancellation is via task cancellation."""
        ...

    async def preload(self, model: str, keep_alive: str = "-1") -> None:
        """Load a model into memory without generating. ``-1`` pins it indefinitely."""
        ...

    async def unload(self, model: str) -> None:
        """Evict a model from memory immediately."""
        ...

    async def aclose(self) -> None:
        """Release network resources."""
        ...
