"""Provider and model discovery.

Discovery answers "what is installed here, and how well can localai drive it". These
tests cover the classification logic and every provider state, using a stub client so
nothing depends on a real Ollama daemon.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from localai.config.models import OllamaConfig
from localai.providers import discovery
from localai.providers.base import ModelInfo
from localai.providers.discovery import (
    DiscoveredProvider,
    ProviderStatus,
    SupportLevel,
    classify_model,
)


def model(name: str, *capabilities: str, **kwargs) -> ModelInfo:
    return ModelInfo(name=name, capabilities=frozenset(capabilities), **kwargs)


# --- classification ---------------------------------------------------------


def test_tool_capable_model_is_full_support() -> None:
    result = classify_model(model("qwen3:8b", "completion", "tools", "thinking"))
    assert result.support is SupportLevel.FULL
    assert result.recommended_for_agent


def test_model_without_tools_falls_back() -> None:
    """No native tool calling still means usable, via the text protocol."""
    result = classify_model(model("gemma3:12b", "completion"))
    assert result.support is SupportLevel.FALLBACK
    assert not result.recommended_for_agent
    assert "less reliable" in result.support.summary


def test_embedding_model_is_not_offered_for_chat() -> None:
    result = classify_model(model("nomic-embed-text", "embedding"))
    assert result.support is SupportLevel.EMBEDDING


def test_embedding_model_that_also_completes_is_not_classed_as_embedding() -> None:
    """Some tags advertise both; chat capability wins because that is what we use."""
    result = classify_model(model("hybrid:7b", "embedding", "completion"))
    assert result.support is not SupportLevel.EMBEDDING


def test_missing_capabilities_assume_fallback() -> None:
    """An older Ollama reports no capabilities. Assume usable rather than useless."""
    result = classify_model(model("mystery:7b"))
    assert result.support is SupportLevel.FALLBACK


@pytest.mark.parametrize(
    ("name", "family", "expected_fragment"),
    [
        ("qwen3:8b", "qwen3", "strong tool calling"),
        ("qwen2.5:7b", "qwen2", "reliable tool calling"),
        ("gemma3:12b", "gemma3", "varies by tag"),
        ("mistral:7b", "mistral", "instruction following"),
    ],
)
def test_known_families_are_annotated(name: str, family: str, expected_fragment: str) -> None:
    result = classify_model(model(name, "completion", family=family))
    assert expected_fragment in result.family_note


def test_unknown_family_gets_no_note_and_is_not_penalised() -> None:
    """An unlisted model must not be treated as inferior."""
    result = classify_model(model("obscure-model:4b", "completion", "tools", family="obscure"))
    assert result.family_note == ""
    assert result.support is SupportLevel.FULL


def test_classification_serialises() -> None:
    data = classify_model(model("qwen3:8b", "completion", "tools")).to_dict()
    assert data["support"] == "full"
    assert data["recommended_for_agent"] is True
    assert "support_summary" in data
    assert data["name"] == "qwen3:8b"


# --- best model selection ---------------------------------------------------


def _provider(*models: ModelInfo) -> DiscoveredProvider:
    return DiscoveredProvider(
        name="ollama",
        status=ProviderStatus.READY,
        detail="test",
        models=[classify_model(m) for m in models],
    )


def test_tool_capable_beats_larger_model_without_tools() -> None:
    """Support level dominates size: a 70B without tools is worse *for this app*."""
    provider = _provider(
        model("huge:70b", "completion", size_bytes=40_000_000_000, context_length=131072),
        model("small:8b", "completion", "tools", size_bytes=5_000_000_000, context_length=8192),
    )
    assert provider.best_model().name == "small:8b"


def test_loaded_model_wins_among_equals() -> None:
    """A resident model answers immediately; that beats a marginally larger window."""
    provider = _provider(
        model("cold:8b", "completion", "tools", context_length=40960),
        model("warm:8b", "completion", "tools", context_length=32768, loaded=True),
    )
    assert provider.best_model().name == "warm:8b"


def test_larger_context_wins_when_neither_is_loaded() -> None:
    provider = _provider(
        model("small-ctx:8b", "completion", "tools", context_length=8192),
        model("big-ctx:27b", "completion", "tools", context_length=262144),
    )
    assert provider.best_model().name == "big-ctx:27b"


def test_best_model_is_none_when_empty() -> None:
    assert _provider().best_model() is None


def test_agent_capable_models_filters_correctly() -> None:
    provider = _provider(
        model("a:8b", "completion", "tools"),
        model("b:8b", "completion"),
        model("c:8b", "completion", "tools"),
    )
    assert {m.name for m in provider.agent_capable_models} == {"a:8b", "c:8b"}


# --- provider probing -------------------------------------------------------


def stub_transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11434"
    )


async def test_ready_provider_reports_models(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.32.0"})
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "qwen3:8b",
                            "size": 5_000_000_000,
                            "capabilities": ["completion", "tools", "thinking"],
                            "details": {"family": "qwen3", "context_length": 40960},
                        },
                        {
                            "name": "gemma3:12b",
                            "size": 7_600_000_000,
                            "capabilities": ["completion"],
                            "details": {"family": "gemma3"},
                        },
                    ]
                },
            )
        return httpx.Response(200, json={"models": []})

    from localai.providers.ollama import OllamaProvider

    monkeypatch.setattr(discovery, "find_ollama_executable", lambda: Path("C:/fake/ollama.exe"))
    original = OllamaProvider.__init__

    def patched(self, config, *, client=None):
        original(self, config, client=stub_transport(handler))

    monkeypatch.setattr(OllamaProvider, "__init__", patched)

    result = await discovery.discover_ollama(OllamaConfig())
    assert result.status is ProviderStatus.READY
    assert result.usable
    assert result.version == "0.32.0"
    assert len(result.models) == 2
    assert len(result.agent_capable_models) == 1
    assert result.best_model().name == "qwen3:8b"
    assert not result.remediation  # nothing to fix


async def test_running_with_no_models_says_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.32.0"})
        return httpx.Response(200, json={"models": []})

    from localai.providers.ollama import OllamaProvider

    monkeypatch.setattr(discovery, "find_ollama_executable", lambda: None)
    original = OllamaProvider.__init__
    monkeypatch.setattr(
        OllamaProvider,
        "__init__",
        lambda self, config, *, client=None: original(self, config, client=stub_transport(handler)),
    )

    result = await discovery.discover_ollama(OllamaConfig())
    assert result.status is ProviderStatus.RUNNING_NO_MODELS
    assert not result.usable
    assert "ollama pull" in result.remediation


async def test_installed_but_not_running_is_distinguished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The remediation differs from 'not installed', so the distinction must survive."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    from localai.providers.ollama import OllamaProvider

    monkeypatch.setattr(discovery, "find_ollama_executable", lambda: Path("C:/fake/ollama.exe"))
    monkeypatch.setattr(discovery, "_binary_version", lambda _: "0.32.0")
    original = OllamaProvider.__init__
    monkeypatch.setattr(
        OllamaProvider,
        "__init__",
        lambda self, config, *, client=None: original(self, config, client=stub_transport(handler)),
    )

    result = await discovery.discover_ollama(OllamaConfig())
    assert result.status is ProviderStatus.INSTALLED_NOT_RUNNING
    assert "ollama serve" in result.remediation
    assert result.version == "0.32.0"


async def test_not_installed_suggests_installing(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    from localai.providers.ollama import OllamaProvider

    monkeypatch.setattr(discovery, "find_ollama_executable", lambda: None)
    original = OllamaProvider.__init__
    monkeypatch.setattr(
        OllamaProvider,
        "__init__",
        lambda self, config, *, client=None: original(self, config, client=stub_transport(handler)),
    )

    result = await discovery.discover_ollama(OllamaConfig())
    assert result.status is ProviderStatus.NOT_INSTALLED
    assert "ollama.com" in result.remediation


async def test_discovery_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every caller reports the outcome; none of them should have to catch."""
    from localai.providers.ollama import OllamaProvider

    def explode(self, config, *, client=None):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(OllamaProvider, "__init__", explode)
    result = await discovery.discover_ollama(OllamaConfig())
    assert result.status is ProviderStatus.UNKNOWN
    assert "something unexpected" in result.detail


# --- summary and model directory --------------------------------------------


def test_summary_shape_is_stable() -> None:
    providers = [_provider(model("qwen3:8b", "completion", "tools", context_length=40960))]
    payload = discovery.summarise(providers)
    assert payload["schema"] == "localai.providers/1"
    assert payload["summary"]["providers_usable"] == 1
    assert payload["summary"]["models_agent_capable"] == 1
    assert payload["summary"]["recommended_model"] == "qwen3:8b"


def test_summary_handles_no_usable_provider() -> None:
    unusable = DiscoveredProvider(
        name="ollama", status=ProviderStatus.NOT_INSTALLED, detail="absent"
    )
    payload = discovery.summarise([unusable])
    assert payload["summary"]["providers_usable"] == 0
    assert payload["summary"]["recommended_model"] is None


def test_model_dir_env_override_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    assert discovery.ollama_model_dir() == tmp_path


def test_model_dir_returns_none_when_the_override_is_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guess must never be reported as fact."""
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "does-not-exist"))
    assert discovery.ollama_model_dir() is None


def test_mock_provider_is_discoverable() -> None:
    result = discovery.discover_mock()
    assert result.usable
    assert any(m.support is SupportLevel.FULL for m in result.models)
    assert any(m.support is SupportLevel.FALLBACK for m in result.models)
