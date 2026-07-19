"""Discovery of model providers and the models already installed on this machine.

Answers two questions the rest of the application keeps needing:

1. **Which providers exist here?** Is Ollama installed, is it running, where does it
   keep its models, and are there other backends worth knowing about?
2. **What is actually installed, and how well can localai drive it?** A model with the
   ``tools`` capability supports the full agent loop; one without it falls back to a
   less reliable text protocol. That distinction matters more than parameter count, so
   discovery reports it directly rather than leaving the user to work it out.

Everything here is read-only and local. Discovery never downloads a model, never
contacts a registry, and never modifies Ollama's state. It reads the local HTTP API and,
where useful, the on-disk model directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from localai.config.models import OllamaConfig
from localai.providers.base import ModelInfo


class ProviderStatus(StrEnum):
    """How usable a provider is right now."""

    READY = "ready"
    """Installed, running, and has at least one model."""

    RUNNING_NO_MODELS = "running_no_models"
    """Daemon responds but nothing is installed."""

    INSTALLED_NOT_RUNNING = "installed_not_running"
    """Binary present but the daemon is not answering."""

    NOT_INSTALLED = "not_installed"

    UNKNOWN = "unknown"
    """Probing failed for a reason we could not classify."""


class SupportLevel(StrEnum):
    """How completely localai can drive a given model.

    This is the practically useful classification. A 70B model without tool calling is
    less useful *for this application* than an 8B model with it, and saying so plainly
    is more helpful than listing capabilities and leaving the user to infer it.
    """

    FULL = "full"
    """Native tool calling: the agent loop works as designed."""

    FALLBACK = "fallback"
    """No native tool calling; the structured text protocol is used instead."""

    CHAT_ONLY = "chat_only"
    """Usable for conversation, but tools are disabled for it."""

    EMBEDDING = "embedding"
    """An embedding model. Not for chat; reserved for indexing (Phase 3)."""

    @property
    def summary(self) -> str:
        return {
            SupportLevel.FULL: "full agent support (native tool calling)",
            SupportLevel.FALLBACK: "tools via text fallback (less reliable)",
            SupportLevel.CHAT_ONLY: "chat only",
            SupportLevel.EMBEDDING: "embeddings only (not for chat)",
        }[self]


#: Model families known to behave well as agents here, with a short note on why.
#: Used only to annotate discovery output -- nothing is gated on this list, and an
#: unlisted model is never treated as inferior.
KNOWN_FAMILIES: dict[str, str] = {
    "qwen3": "strong tool calling, optional reasoning mode",
    "qwen2": "reliable tool calling",
    "llama": "widely supported; tool calling varies by tag",
    "mistral": "good instruction following",
    "gemma": "compact; tool calling varies by tag",
    "phi": "small and fast",
    "granite": "tool calling supported",
    "command-r": "built for tool use and retrieval",
    "deepseek2": "strong reasoning",
    "nomic-bert": "embedding model",
}


@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    """One installed model, annotated with how well localai can use it."""

    info: ModelInfo
    support: SupportLevel
    family_note: str = ""

    @property
    def name(self) -> str:
        return self.info.name

    @property
    def recommended_for_agent(self) -> bool:
        return self.support is SupportLevel.FULL

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.info.to_dict(),
            "support": self.support.value,
            "support_summary": self.support.summary,
            "recommended_for_agent": self.recommended_for_agent,
            "family_note": self.family_note,
        }


@dataclass(slots=True)
class DiscoveredProvider:
    """One provider backend found on this machine."""

    name: str
    status: ProviderStatus
    detail: str
    version: str = ""
    executable: Path | None = None
    base_url: str = ""
    model_dir: Path | None = None
    models: list[DiscoveredModel] = field(default_factory=list)
    remediation: str = ""

    @property
    def usable(self) -> bool:
        return self.status is ProviderStatus.READY

    @property
    def agent_capable_models(self) -> list[DiscoveredModel]:
        return [m for m in self.models if m.recommended_for_agent]

    def best_model(self) -> DiscoveredModel | None:
        """The model most suitable for agentic use.

        Preference order: full tool support first, then already resident in memory
        (no load delay), then larger context, then larger weights. Load state is
        ranked above context length deliberately -- a model that is already loaded
        starts answering immediately, which matters more in interactive use than a
        marginally larger window.
        """
        if not self.models:
            return None
        support_rank = {
            SupportLevel.FULL: 3,
            SupportLevel.FALLBACK: 2,
            SupportLevel.CHAT_ONLY: 1,
            SupportLevel.EMBEDDING: 0,
        }
        return max(
            self.models,
            key=lambda m: (
                support_rank[m.support],
                m.info.loaded,
                m.info.context_length,
                m.info.size_bytes,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        best = self.best_model()
        return {
            "name": self.name,
            "status": self.status.value,
            "usable": self.usable,
            "detail": self.detail,
            "version": self.version,
            "executable": str(self.executable) if self.executable else None,
            "base_url": self.base_url,
            "model_dir": str(self.model_dir) if self.model_dir else None,
            "remediation": self.remediation,
            "model_count": len(self.models),
            "agent_capable_count": len(self.agent_capable_models),
            "recommended_model": best.name if best else None,
            "models": [m.to_dict() for m in self.models],
        }


def classify_model(info: ModelInfo) -> DiscoveredModel:
    """Decide how completely localai can drive one model."""
    if info.supports_embedding and not info.capabilities & {"completion", "tools"}:
        support = SupportLevel.EMBEDDING
    elif info.supports_tools:
        support = SupportLevel.FULL
    elif "completion" in info.capabilities or not info.capabilities:
        # An empty capability set means an older Ollama that does not report them.
        # Assuming chat-with-fallback is the useful default: the fallback protocol
        # works on any instruction-following model, and the UI says it is in use.
        support = SupportLevel.FALLBACK
    else:
        support = SupportLevel.CHAT_ONLY

    family = (info.family or info.name.split(":")[0]).lower()
    note = next((v for k, v in KNOWN_FAMILIES.items() if family.startswith(k)), "")
    return DiscoveredModel(info=info, support=support, family_note=note)


def ollama_model_dir() -> Path | None:
    """Locate Ollama's model store, so the user can see where the disk is going.

    ``OLLAMA_MODELS`` wins if set; otherwise Ollama uses ``~/.ollama/models`` on every
    platform. Returned only if it exists, so a wrong guess is never reported as fact.
    """
    if override := os.environ.get("OLLAMA_MODELS"):
        candidate = Path(override)
        return candidate if candidate.exists() else None

    candidates = [Path.home() / ".ollama" / "models"]
    if sys.platform == "win32" and (local := os.environ.get("LOCALAPPDATA")):
        candidates.append(Path(local) / "Ollama" / "models")
    return next((c for c in candidates if c.exists()), None)


def find_ollama_executable() -> Path | None:
    """Find the ollama binary, checking PATH then the usual install locations."""
    if located := shutil.which("ollama"):
        return Path(located)

    candidates: list[Path] = []
    if sys.platform == "win32":
        for var in ("LOCALAPPDATA", "ProgramFiles"):
            if base := os.environ.get(var):
                candidates.append(Path(base) / "Programs" / "Ollama" / "ollama.exe")
                candidates.append(Path(base) / "Ollama" / "ollama.exe")
    else:
        candidates += [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")]
    return next((c for c in candidates if c.exists()), None)


def _binary_version(executable: Path) -> str:
    """Read the version from the binary, for the case where the daemon is down."""
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        text = (result.stdout or result.stderr).strip()
        # "ollama version is 0.32.0" -> "0.32.0"
        return text.split()[-1] if text else ""
    except (OSError, subprocess.SubprocessError):
        return ""


async def discover_ollama(config: OllamaConfig | None = None) -> DiscoveredProvider:
    """Probe the local Ollama installation.

    Never raises: an unreachable or absent daemon is a *result*, not an error, because
    every caller (doctor, the CLI, first-run setup) wants to report it rather than
    crash on it.
    """
    from localai.providers.ollama import OllamaProvider

    settings = config or OllamaConfig()
    executable = find_ollama_executable()
    model_dir = ollama_model_dir()

    # Construction is inside the try because it can fail too -- an invalid host, or a
    # transport that cannot be created. The "never raises" contract covers the whole
    # function, not just the network calls.
    provider = None
    try:
        provider = OllamaProvider(settings)
        reachable, detail = await provider.health()

        if not reachable:
            if executable is None:
                return DiscoveredProvider(
                    name="ollama",
                    status=ProviderStatus.NOT_INSTALLED,
                    detail="Ollama was not found on this machine",
                    base_url=settings.base_url,
                    model_dir=model_dir,
                    remediation=(
                        "Install it from https://ollama.com/download, or run: "
                        "winget install Ollama.Ollama"
                    ),
                )
            return DiscoveredProvider(
                name="ollama",
                status=ProviderStatus.INSTALLED_NOT_RUNNING,
                detail=detail,
                version=_binary_version(executable),
                executable=executable,
                base_url=settings.base_url,
                model_dir=model_dir,
                remediation="Start it with 'ollama serve', or launch the Ollama desktop app.",
            )

        version = await provider.version()
        models = [classify_model(info) for info in await provider.list_models()]
        models.sort(key=lambda m: (m.support is not SupportLevel.FULL, not m.info.loaded, m.name))

        if not models:
            return DiscoveredProvider(
                name="ollama",
                status=ProviderStatus.RUNNING_NO_MODELS,
                detail="running, but no models are installed",
                version=version,
                executable=executable,
                base_url=settings.base_url,
                model_dir=model_dir,
                remediation="Install one with: ollama pull qwen3:8b",
            )

        agent_capable = sum(1 for m in models if m.recommended_for_agent)
        remediation = (
            ""
            if agent_capable
            else (
                "None of your models support native tool calling. localai will use a "
                "text fallback, which is less reliable. For the full agent experience: "
                "ollama pull qwen3:8b"
            )
        )
        return DiscoveredProvider(
            name="ollama",
            status=ProviderStatus.READY,
            detail=f"{len(models)} model(s), {agent_capable} with native tool calling",
            version=version,
            executable=executable,
            base_url=settings.base_url,
            model_dir=model_dir,
            models=models,
            remediation=remediation,
        )
    except Exception as exc:  # discovery must never propagate
        return DiscoveredProvider(
            name="ollama",
            status=ProviderStatus.UNKNOWN,
            detail=f"{type(exc).__name__}: {exc}",
            executable=executable,
            base_url=settings.base_url,
            model_dir=model_dir,
            remediation="Run 'ai doctor' for a fuller diagnosis.",
        )
    finally:
        if provider is not None:
            await provider.aclose()


def discover_mock() -> DiscoveredProvider:
    """The built-in deterministic provider. Always available, never the default."""
    from localai.providers.mock import DEFAULT_MODELS

    models = [classify_model(info) for info in DEFAULT_MODELS]
    return DiscoveredProvider(
        name="mock",
        status=ProviderStatus.READY,
        detail="built-in deterministic provider for tests and the dev sandbox",
        version="mock-0",
        models=models,
    )


async def discover_all(
    config: OllamaConfig | None = None, *, include_mock: bool = False
) -> list[DiscoveredProvider]:
    """Probe every provider backend localai knows about.

    Only Ollama ships today. The list shape is stable so adding a backend later does
    not change the contract for callers.
    """
    providers = [await discover_ollama(config)]
    if include_mock:
        providers.append(discover_mock())
    return providers


async def find_best_model(config: OllamaConfig | None = None) -> DiscoveredModel | None:
    """The single most suitable installed model for agentic use, or None.

    Used for first-run defaults: rather than picking whatever happens to be first
    alphabetically, pick the one that will actually work well.
    """
    return best_across(await discover_all(config))


def best_across(providers: Sequence[DiscoveredProvider]) -> DiscoveredModel | None:
    """The single best model across every usable provider, or None."""
    candidates = [best for p in providers if p.usable and (best := p.best_model()) is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda m: (m.recommended_for_agent, m.info.context_length))


def summarise(providers: Sequence[DiscoveredProvider]) -> dict[str, Any]:
    """Build the document emitted by ``ai providers scan --json``."""
    usable = [p for p in providers if p.usable]
    all_models = [m for p in providers for m in p.models]
    recommended = best_across(providers)
    return {
        "schema": "localai.providers/1",
        "providers": [p.to_dict() for p in providers],
        "summary": {
            "providers_found": len(providers),
            "providers_usable": len(usable),
            "models_total": len(all_models),
            "models_agent_capable": sum(1 for m in all_models if m.recommended_for_agent),
            "recommended_model": recommended.name if recommended else None,
        },
    }


__all__ = [
    "DiscoveredModel",
    "DiscoveredProvider",
    "ProviderStatus",
    "SupportLevel",
    "best_across",
    "classify_model",
    "discover_all",
    "discover_ollama",
    "find_best_model",
    "find_ollama_executable",
    "ollama_model_dir",
    "summarise",
]
