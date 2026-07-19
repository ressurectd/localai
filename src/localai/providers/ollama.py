"""Ollama HTTP client.

Talks to the local daemon over loopback. The only network calls the application makes
by default are the ones in this module, and they go to ``ollama.base_url``.

Endpoints used: ``/api/version``, ``/api/tags`` (installed), ``/api/ps`` (loaded),
``/api/show`` (detail), ``/api/chat`` (streaming NDJSON), ``/api/generate`` (preload).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Any

import httpx

from localai.config.models import OllamaConfig
from localai.domain.messages import Message, ToolCall, Usage
from localai.errors import ModelNotFoundError, ProviderError, ProviderUnavailableError
from localai.providers.base import ChatChunk, ModelInfo

log = logging.getLogger(__name__)


def _model_info_from_tags(entry: dict[str, Any]) -> ModelInfo:
    """Build a ModelInfo from one ``/api/tags`` entry.

    Ollama 0.32+ includes ``capabilities`` and ``details.context_length`` here, which
    lets us describe every installed model from a single request. Older daemons omit
    them; the fields default to empty and :meth:`OllamaProvider.show_model` fills the
    gap on demand rather than us issuing N requests up front.
    """
    details = entry.get("details") or {}
    return ModelInfo(
        name=entry.get("name") or entry.get("model", ""),
        size_bytes=int(entry.get("size") or 0),
        family=details.get("family", ""),
        parameter_size=details.get("parameter_size", ""),
        quantization=details.get("quantization_level", ""),
        context_length=int(details.get("context_length") or 0),
        embedding_length=int(details.get("embedding_length") or 0),
        capabilities=frozenset(entry.get("capabilities") or ()),
        modified_at=entry.get("modified_at", ""),
    )


class OllamaProvider:
    """Async Ollama client implementing :class:`~localai.providers.base.ModelProvider`."""

    name = "ollama"

    def __init__(self, config: OllamaConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(config.request_timeout_s, connect=config.connect_timeout_s),
            # Ollama is local; a proxy would silently route local traffic off-device,
            # which would violate the project's local-only guarantee.
            trust_env=False,
        )

    # -- lifecycle ------------------------------------------------------------

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> OllamaProvider:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- introspection --------------------------------------------------------

    async def health(self) -> tuple[bool, str]:
        """Probe the daemon. Contractually never raises, so `doctor` can report cleanly."""
        try:
            response = await self._client.get("/api/version", timeout=self.config.connect_timeout_s)
            response.raise_for_status()
            return True, f"ollama {response.json().get('version', 'unknown')}"
        except httpx.ConnectError:
            return False, f"cannot connect to {self.config.base_url}"
        except httpx.TimeoutException:
            return False, f"timed out connecting to {self.config.base_url}"
        except Exception as exc:  # defensive: health must not propagate
            return False, f"{type(exc).__name__}: {exc}"

    async def version(self) -> str:
        data = await self._get_json("/api/version")
        return str(data.get("version", ""))

    async def list_models(self) -> list[ModelInfo]:
        """Installed models, cross-referenced with ``/api/ps`` for live load state."""
        data = await self._get_json("/api/tags")
        models = [_model_info_from_tags(entry) for entry in data.get("models", [])]

        # Annotating with load state is a convenience, not a requirement: if /api/ps
        # is unavailable on an older daemon we still return the full model list.
        try:
            resident = {m.name: m for m in await self.loaded_models()}
        except ProviderError:
            log.debug("could not read /api/ps; load state omitted", exc_info=True)
            return models

        return [
            (
                replace(
                    m,
                    loaded=True,
                    vram_bytes=resident[m.name].vram_bytes,
                    expires_at=resident[m.name].expires_at,
                )
                if m.name in resident
                else m
            )
            for m in models
        ]

    async def loaded_models(self) -> list[ModelInfo]:
        data = await self._get_json("/api/ps")
        out: list[ModelInfo] = []
        for entry in data.get("models", []):
            out.append(
                replace(
                    _model_info_from_tags(entry),
                    loaded=True,
                    vram_bytes=int(entry.get("size_vram") or entry.get("size") or 0),
                    expires_at=entry.get("expires_at", ""),
                )
            )
        return out

    async def show_model(self, name: str) -> ModelInfo:
        """Detailed model info, merging ``/api/show`` over the ``/api/tags`` entry."""
        try:
            response = await self._client.post("/api/show", json={"model": name})
            if response.status_code == 404:
                raise ModelNotFoundError(
                    f"model {name!r} is not installed",
                    remediation=f"Install it with: ollama pull {name}",
                    model=name,
                )
            response.raise_for_status()
            detail = response.json()
        except httpx.HTTPError as exc:
            raise self._unavailable(exc) from exc

        base = next((m for m in await self.list_models() if m.name == name), None)
        info = detail.get("model_info") or {}
        # Ollama namespaces architecture keys, e.g. "qwen3.context_length".
        ctx = next(
            (int(v) for k, v in info.items() if k.endswith(".context_length") and v),
            base.context_length if base else 0,
        )
        details = detail.get("details") or {}
        return ModelInfo(
            name=name,
            size_bytes=base.size_bytes if base else 0,
            family=details.get("family", "") or (base.family if base else ""),
            parameter_size=details.get("parameter_size", ""),
            quantization=details.get("quantization_level", ""),
            context_length=ctx,
            embedding_length=next(
                (int(v) for k, v in info.items() if k.endswith(".embedding_length") and v), 0
            ),
            capabilities=frozenset(detail.get("capabilities") or ()),
            modified_at=base.modified_at if base else "",
            loaded=base.loaded if base else False,
            vram_bytes=base.vram_bytes if base else 0,
        )

    # -- generation -----------------------------------------------------------

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
        """Stream a chat completion as :class:`ChatChunk` objects.

        Cancellation works by cancelling the consuming task: httpx tears the
        connection down, and Ollama stops generating shortly afterwards.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.to_ollama() for m in messages],
            "stream": True,
            "keep_alive": keep_alive or self.config.default_keep_alive,
        }
        if tools:
            payload["tools"] = list(tools)
        if options:
            payload["options"] = options
        if think is not None:
            payload["think"] = think
        if images:
            # Ollama attaches images to the most recent user message.
            import base64

            for message in reversed(payload["messages"]):
                if message["role"] == "user":
                    message["images"] = [base64.b64encode(i).decode("ascii") for i in images]
                    break

        thinking_accumulated: list[str] = []
        try:
            async with self._client.stream(
                "POST", "/api/chat", json=payload, timeout=self.config.request_timeout_s
            ) as response:
                if response.status_code == 404:
                    await response.aread()
                    raise ModelNotFoundError(
                        f"model {model!r} is not installed",
                        remediation=f"Install it with: ollama pull {model}",
                        model=model,
                    )
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:500]
                    raise ProviderError(
                        f"ollama returned HTTP {response.status_code}: {body}",
                        remediation="Check the Ollama server log for details.",
                        status=response.status_code,
                    )

                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        # A malformed line is worth noting but not worth aborting a
                        # long generation over; the stream usually recovers.
                        log.warning("skipping malformed NDJSON line from ollama: %.120s", line)
                        continue

                    if error := chunk.get("error"):
                        raise ProviderError(
                            f"ollama error during generation: {error}",
                            remediation="Try a smaller context, or check available memory.",
                        )

                    message = chunk.get("message") or {}
                    if thinking := message.get("thinking"):
                        thinking_accumulated.append(thinking)

                    if chunk.get("done"):
                        yield ChatChunk(
                            done=True,
                            model=chunk.get("model", model),
                            usage=Usage.from_ollama_final(chunk, "".join(thinking_accumulated)),
                        )
                        return

                    yield ChatChunk(
                        content=message.get("content", "") or "",
                        thinking=thinking or "",
                        tool_calls=[
                            ToolCall.from_ollama(tc) for tc in (message.get("tool_calls") or [])
                        ],
                        model=chunk.get("model", model),
                    )
        except httpx.HTTPError as exc:
            raise self._unavailable(exc) from exc

    async def preload(self, model: str, keep_alive: str = "-1") -> None:
        """Load a model into memory. An empty prompt makes this a pure load."""
        await self._post_json("/api/generate", {"model": model, "keep_alive": keep_alive})

    async def unload(self, model: str) -> None:
        """Evict a model. ``keep_alive: 0`` is Ollama's documented unload signal."""
        await self._post_json("/api/generate", {"model": model, "keep_alive": 0})

    # -- internals ------------------------------------------------------------

    def _unavailable(self, exc: Exception) -> ProviderError:
        """Translate a transport failure into an actionable application error."""
        if isinstance(exc, ProviderError):
            return exc
        return ProviderUnavailableError(
            f"cannot reach Ollama at {self.config.base_url}: {exc}",
            remediation=(
                "Start Ollama ('ollama serve', or launch the desktop app), then retry. "
                "Verify the host and port with: ai doctor"
            ),
            base_url=self.config.base_url,
        )

    async def _get_json(self, path: str) -> dict[str, Any]:
        try:
            response = await self._client.get(path)
            response.raise_for_status()
            return dict(response.json())
        except httpx.HTTPError as exc:
            raise self._unavailable(exc) from exc

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=payload)
            response.raise_for_status()
            text = response.text.strip()
            return dict(json.loads(text.splitlines()[-1])) if text else {}
        except httpx.HTTPError as exc:
            raise self._unavailable(exc) from exc
        except (json.JSONDecodeError, IndexError):
            return {}
