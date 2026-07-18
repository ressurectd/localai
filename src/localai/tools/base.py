"""The tool contract.

Every capability the model can invoke is a :class:`Tool`. A tool declares its risk,
whether it mutates state, whether it touches the network, and a JSON Schema for its
arguments. The registry validates arguments against that schema and the permissions
engine reads the risk and paths -- so a tool cannot accidentally bypass policy by
forgetting to ask, because it never asks in the first place. The runner does.

To add a tool, see docs/tool-api.md.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from localai.config.models import Config, RiskLevel
from localai.config.paths import AppPaths
from localai.version import TOOL_API_VERSION


@dataclass(slots=True)
class ToolContext:
    """Ambient state a tool may read. Deliberately narrow.

    A tool receives configuration and the working directory but *not* the permissions
    engine: tools must not be able to consult, and therefore cannot be tempted to
    second-guess, the policy decision that already permitted them to run.
    """

    config: Config
    paths: AppPaths
    cwd: Path
    workspaces: tuple[Path, ...] = ()
    conversation_id: str | None = None
    dry_run: bool = False
    cancel: asyncio.Event | None = None

    def cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()


@dataclass(slots=True)
class ToolResult:
    """What a tool returns. Serialised to the model and rendered in the UI."""

    ok: bool = True
    content: str = ""
    """Text placed in the model's context. Truncated to the configured limit."""

    error: str | None = None
    truncated: bool = False
    full_output_path: Path | None = None
    """Where the untruncated output was spilled, when truncation occurred."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Structured detail for the UI: counts, sizes, exit codes. Not sent to the model."""

    flags: list[str] = field(default_factory=list)
    """Advisory markers, e.g. ``prompt_injection_suspected``, ``dry_run``."""

    untrusted: bool = False
    """True when content originated from file or command output rather than the tool itself."""

    @classmethod
    def failure(cls, error: str, **metadata: Any) -> ToolResult:
        return cls(ok=False, error=error, content=f"Error: {error}", metadata=metadata)

    def to_dict(self) -> dict[str, Any]:
        """Matches ``schemas/tool-result.schema.json``."""
        return {
            "schema_version": TOOL_API_VERSION,
            "ok": self.ok,
            "content": self.content,
            "error": self.error,
            "truncated": self.truncated,
            "full_output_path": str(self.full_output_path) if self.full_output_path else None,
            "metadata": self.metadata,
            "flags": self.flags,
            "untrusted": self.untrusted,
        }


class Tool(ABC):
    """Base class for all tools.

    Subclasses set the class attributes and implement :meth:`run`. Everything else --
    permission evaluation, argument validation, truncation, auditing, injection
    scanning -- happens in :class:`~localai.tools.runner.ToolRunner`, so a tool
    author cannot forget to do it.
    """

    #: Stable identifier exposed to the model. Never renamed without a deprecation.
    name: str = ""

    #: Shown to the model. Write it for the model, not for a human reader: state
    #: what the tool does, when to use it, and what it returns.
    description: str = ""

    risk: RiskLevel = RiskLevel.READ
    mutating: bool = False
    network: bool = False

    #: True when the tool's output contains file or command content the model should
    #: treat as data. The runner wraps such output in an untrusted-content envelope.
    returns_untrusted_content: bool = False

    #: JSON Schema (draft 2020-12) for the arguments object.
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    #: Free-form grouping used by ``localai tools list`` and MCP tool filtering.
    category: str = "general"

    @abstractmethod
    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute. Raise only for genuinely unexpected failures.

        Expected failures (file missing, command exited non-zero) should be returned
        as ``ToolResult.failure`` so the model can read the error and adapt.
        """

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        """Paths this call would touch, for the permissions engine.

        Getting this wrong is a security bug: a path the engine never sees is a path
        it cannot contain. Tests in ``tests/unit/test_tool_paths.py`` assert that
        every path-taking tool declares its paths.
        """
        return []

    def describe_call(self, arguments: dict[str, Any]) -> str:
        """One-line human-readable preview, shown *before* execution.

        This is what the user sees in the confirmation prompt, so it must reflect
        what will actually happen -- not a generic summary.
        """
        rendered = ", ".join(f"{k}={_abbreviate(v)}" for k, v in arguments.items())
        return f"{self.name}({rendered})"

    def to_ollama_schema(self) -> dict[str, Any]:
        """Render as an Ollama/OpenAI-style function definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        """Matches ``schemas/tool-definition.schema.json``."""
        return {
            "schema_version": TOOL_API_VERSION,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "risk": self.risk.value,
            "mutating": self.mutating,
            "network": self.network,
            "returns_untrusted_content": self.returns_untrusted_content,
            "parameters": self.parameters,
        }


def _abbreviate(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."
