"""Composition root.

Every dependency is constructed here and injected downward. Nothing else in the
project reaches for a global: the TUI, the CLI and (later) the API and MCP server
each build an :class:`Application` and use it. That is what makes it possible to
run the whole system against a mock provider and a temporary config directory in a
test, with no monkey-patching.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from localai.config.manager import ConfigManager
from localai.config.models import Config, ModelProfile, PermissionMode
from localai.config.paths import AppPaths
from localai.permissions.audit import AuditLogger
from localai.permissions.engine import Caller, Interface, PermissionEngine
from localai.providers.base import ModelProvider
from localai.providers.mock import MockProvider
from localai.providers.ollama import OllamaProvider
from localai.storage.audit_store import AuditStore
from localai.storage.conversations import ConversationStore
from localai.storage.db import Database
from localai.storage.usage import UsageStore
from localai.tools.base import ToolContext
from localai.tools.builtin import register_builtins
from localai.tools.registry import ToolRegistry
from localai.tools.runner import ConfirmCallback, ToolRunner

log = logging.getLogger(__name__)


@dataclass
class Application:
    """The wired-up application. Construct with :meth:`create`."""

    paths: AppPaths
    config_manager: ConfigManager
    database: Database
    conversations: ConversationStore
    usage: UsageStore
    registry: ToolRegistry
    engine: PermissionEngine
    audit: AuditLogger
    runner: ToolRunner
    provider: ModelProvider
    cwd: Path
    caller: Caller = field(default_factory=Caller)

    @property
    def config(self) -> Config:
        return self.config_manager.config

    @classmethod
    def create(
        cls,
        *,
        home: Path | None = None,
        cwd: Path | None = None,
        interface: Interface = Interface.CLI,
        client_id: str = "local-user",
        provider: ModelProvider | None = None,
        use_mock: bool = False,
        confirm: ConfirmCallback | None = None,
        overrides: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
    ) -> Application:
        """Build a fully wired application.

        ``overrides`` applies CLI flags on top of the loaded configuration -- the
        highest-precedence layer. ``use_mock`` swaps in the deterministic provider,
        which is what ``localai dev sandbox`` and the test suite use.
        """
        paths = AppPaths.resolve(home).ensure()
        config_manager = ConfigManager(paths, env=env)
        config = config_manager.load()

        if overrides:
            config = _apply_overrides(config, overrides)
            config_manager._config = config

        database = Database(paths.database)
        audit = AuditLogger(paths.audit_log, AuditStore(database))
        engine = PermissionEngine(config, paths)

        # In read-only or investigation mode the mutating tools are never registered,
        # so they cannot be invoked even if a policy check were bypassed.
        registry = register_builtins(include_mutating=not config.safety.read_only)

        resolved_provider: ModelProvider = provider or (
            MockProvider() if use_mock else OllamaProvider(config.ollama)
        )
        return cls(
            paths=paths,
            config_manager=config_manager,
            database=database,
            conversations=ConversationStore(database),
            usage=UsageStore(database),
            registry=registry,
            engine=engine,
            audit=audit,
            runner=ToolRunner(registry, engine, audit, confirm=confirm),
            provider=resolved_provider,
            cwd=(cwd or Path.cwd()).resolve(),
            caller=Caller(interface=interface, client_id=client_id),
        )

    def tool_context(
        self, *, conversation_id: str | None = None, cancel: Any = None
    ) -> ToolContext:
        """Build the context handed to every tool this session runs."""
        return ToolContext(
            config=self.config,
            paths=self.paths,
            cwd=self.cwd,
            workspaces=self.engine.workspace_paths(),
            conversation_id=conversation_id,
            dry_run=self.config.safety.dry_run,
            cancel=cancel,
        )

    def resolve_profile(self, name: str | None) -> ModelProfile | None:
        """Look up a model profile by name, falling back to the configured default."""
        key = name or self.config.default_profile
        return self.config.profiles.get(key) if key else None

    def set_mode(self, mode: PermissionMode) -> None:
        """Change permission mode and record the change in the audit log.

        Mode changes are audited because "when did this become permissive?" is a
        question the log has to be able to answer.
        """
        previous = self.engine.mode
        self.engine.set_mode(mode)
        self.audit.record_event(
            "permission_mode_changed",
            self.caller,
            reason=f"{previous.value} -> {mode.value}",
            previous=previous.value,
            current=mode.value,
        )

    def set_kill_switch(self, engaged: bool) -> None:
        """Engage or clear the global kill switch, with an audit record."""
        self.config.permissions.kill_switch = engaged
        self.audit.record_event(
            "kill_switch_engaged" if engaged else "kill_switch_cleared",
            self.caller,
            reason="all mutating and executing tools disabled" if engaged else "restored",
        )

    async def aclose(self) -> None:
        await self.provider.aclose()
        self.database.close()

    async def __aenter__(self) -> Application:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def _apply_overrides(config: Config, overrides: dict[str, Any]) -> Config:
    """Apply CLI-level overrides by revalidating, so invalid flags fail loudly."""
    data = config.model_dump()
    for dotted, value in overrides.items():
        if value is None:
            continue
        *parents, leaf = dotted.split(".")
        cursor: Any = data
        for part in parents:
            cursor = cursor.setdefault(part, {})
        cursor[leaf] = value
    return Config.model_validate(data)
