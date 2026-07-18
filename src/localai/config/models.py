"""Typed configuration model.

Pydantic is the single source of truth for configuration: the TOML file, the
environment-variable overrides, the ``config.schema.json`` published for external
agents and the in-UI settings editor all derive from these classes. There is no
prose-only setting anywhere in the project.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from localai.version import CONFIG_SCHEMA_VERSION


class PermissionMode(StrEnum):
    """How much autonomy tool calls are granted by default.

    These are *defaults*: an explicit deny rule still wins in every mode, and the
    protected-path and kill-switch checks run before the mode is even consulted.
    """

    MANUAL = "manual"
    """Confirm every tool call, including read-only ones."""

    AUTO = "auto"
    """Auto-approve read-only actions; confirm mutation, execution and privileged ones."""

    WORKSPACE = "workspace"
    """Auto-approve anything inside a trusted workspace; confirm everything outside it."""

    BYPASS = "bypass"
    """No per-call confirmation. Still fully logged, displayed and audited."""


class RiskLevel(StrEnum):
    """Risk classification assigned to each tool, ordered by severity.

    All four comparison operators are defined explicitly and compare :attr:`rank`.
    This is not optional decoration: ``StrEnum`` inherits ``str``'s comparisons, so
    omitting any one of them silently falls back to *alphabetical* ordering --
    ``"read" >= "execute"`` is True because 'r' sorts after 'e'. That would make
    ``risk >= RiskLevel.EXECUTE`` accept a read, and every severity gate in the
    permissions engine would compare spelling instead of severity.
    """

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    EXECUTE = "execute"
    PRIVILEGED = "privileged"

    @property
    def rank(self) -> int:
        return _RISK_ORDER[self]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.rank >= other.rank


_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.READ: 10,
    RiskLevel.WRITE: 20,
    RiskLevel.DESTRUCTIVE: 30,
    RiskLevel.EXECUTE: 40,
    RiskLevel.PRIVILEGED: 50,
}


class StrictModel(BaseModel):
    """Base config model: unknown keys are an error, not a silent no-op.

    A typo in ``config.toml`` should fail loudly at ``localai config validate`` rather
    than leaving the user to wonder why a setting had no effect.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class GenerationOptions(StrictModel):
    """Ollama generation parameters. ``None`` means "let Ollama use its default"."""

    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = 0.7
    top_p: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    top_k: Annotated[int, Field(ge=0)] | None = None
    min_p: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    repeat_penalty: Annotated[float, Field(ge=0.0)] | None = None
    seed: int | None = Field(default=None, description="Set for reproducible sampling.")
    num_ctx: Annotated[int, Field(ge=256)] | None = Field(
        default=None, description="Context window in tokens. Defaults to the model's own."
    )
    num_predict: int | None = Field(default=None, description="-1 for unlimited.")
    stop: list[str] = Field(default_factory=list)

    def to_ollama(self) -> dict[str, Any]:
        """Render as Ollama's ``options`` object, omitting unset values."""
        raw = self.model_dump(exclude_none=True)
        if not raw.get("stop"):
            raw.pop("stop", None)
        return raw


class ModelProfile(StrictModel):
    """A named, reusable bundle of model + prompt + generation settings."""

    name: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    model: str = Field(description="Ollama model tag, e.g. 'qwen3:8b'.")
    description: str = ""
    system_prompt: str | None = None
    think: bool | Literal["low", "medium", "high"] | None = Field(
        default=None,
        description="Enable reasoning on models that support it. None = model default.",
    )
    keep_alive: str | None = Field(
        default=None, description="Ollama keep_alive, e.g. '30m' or '-1' to pin in memory."
    )
    options: GenerationOptions = Field(default_factory=GenerationOptions)


class OllamaConfig(StrictModel):
    """Connection settings for the local Ollama daemon."""

    host: str = Field(default="127.0.0.1", description="Loopback by default. See privacy docs.")
    port: Annotated[int, Field(ge=1, le=65535)] = 11434
    scheme: Literal["http", "https"] = "http"
    request_timeout_s: Annotated[float, Field(gt=0)] = 120.0
    connect_timeout_s: Annotated[float, Field(gt=0)] = 5.0
    default_keep_alive: str = "5m"

    @field_validator("host")
    @classmethod
    def _warn_on_non_loopback(cls, value: str) -> str:
        # Not an error: some users legitimately run Ollama on another machine. But the
        # value is recorded so `doctor` and the status bar can flag it as off-device.
        return value

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "::1", "localhost"}

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


class WorkspaceConfig(StrictModel):
    """A directory the user has explicitly designated as trusted."""

    path: Path
    name: str = ""
    allow_write: bool = True
    allow_execute: bool = False
    """Shell execution inside a trusted workspace still requires opting in per workspace."""

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        expanded = value.expanduser()
        if not expanded.is_absolute():
            raise ValueError(f"workspace path must be absolute, got {value!r}")
        return expanded


class PermissionRuleModel(StrictModel):
    """A persisted permission rule. Mirrors ``schemas/permission-rule.schema.json``."""

    id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,64}$")
    effect: Literal["allow", "deny", "confirm"]
    note: str = ""
    tools: list[str] = Field(
        default_factory=list, description="Tool names or fnmatch globs. Empty = any tool."
    )
    paths: list[str] = Field(default_factory=list, description="Path globs. Empty = any path.")
    command_patterns: list[str] = Field(
        default_factory=list, description="fnmatch globs matched against the full command line."
    )
    max_risk: RiskLevel | None = Field(
        default=None, description="Rule applies only at or below this risk level."
    )
    interfaces: list[Literal["tui", "cli", "api", "mcp"]] = Field(
        default_factory=list, description="Empty = every interface."
    )
    allow_sensitive: bool = Field(
        default=False,
        description="Required for an allow rule to cover credential-bearing paths.",
    )
    expires_at: float | None = Field(default=None, description="Unix timestamp; None = never.")

    @model_validator(mode="after")
    def _reject_empty_allow(self) -> PermissionRuleModel:
        # An allow rule with no selectors would silently grant everything. Deny and
        # confirm rules are permitted to be broad because they only ever restrict.
        if self.effect == "allow" and not (self.tools or self.paths or self.command_patterns):
            raise ValueError(
                f"allow rule {self.id!r} must constrain at least one of "
                "tools/paths/command_patterns; a rule with no selectors would grant everything"
            )
        return self


class PermissionsConfig(StrictModel):
    """Permission policy: mode, workspaces, rules and the global kill switch."""

    mode: PermissionMode = PermissionMode.AUTO
    workspaces: list[WorkspaceConfig] = Field(default_factory=list)
    rules: list[PermissionRuleModel] = Field(default_factory=list)

    kill_switch: bool = Field(
        default=False,
        description="When true, every mutating and executing tool is denied on every interface.",
    )
    confirm_outside_workspace: bool = Field(
        default=True, description="Require confirmation for reads outside a trusted workspace."
    )
    bypass_confirmation_phrase: str = Field(
        default="I understand the risk",
        description="Typed verbatim to enter bypass mode. Never stored as accepted.",
    )
    max_high_risk_per_minute: Annotated[int, Field(ge=0)] = Field(
        default=20, description="Rate limit on execute/destructive actions. 0 disables."
    )

    @model_validator(mode="after")
    def _unique_rule_ids(self) -> PermissionsConfig:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"duplicate permission rule id: {rule.id!r}")
            seen.add(rule.id)
        return self


class SafetyConfig(StrictModel):
    """Reversibility and blast-radius controls, independent of permission mode."""

    dry_run: bool = Field(default=False, description="Simulate mutations; never write.")
    read_only: bool = Field(default=False, description="Deny every non-read tool outright.")
    backup_before_modify: bool = True
    use_recycle_bin: bool = Field(
        default=True, description="Delete to the Recycle Bin rather than permanently."
    )
    git_checkpoints: bool = Field(
        default=False, description="Commit a checkpoint to the workspace repo before mutations."
    )
    diff_preview: bool = True
    tool_timeout_s: Annotated[float, Field(gt=0)] = 60.0
    shell_timeout_s: Annotated[float, Field(gt=0)] = 120.0
    max_output_bytes: Annotated[int, Field(ge=1024)] = Field(
        default=200_000, description="Output above this is truncated; the full log is kept on disk."
    )
    max_read_bytes: Annotated[int, Field(ge=1024)] = 5_000_000
    max_scan_depth: Annotated[int, Field(ge=1)] = 25
    max_scan_entries: Annotated[int, Field(ge=1)] = 50_000
    follow_symlinks: bool = Field(
        default=False, description="Off by default: prevents junction escapes from workspaces."
    )
    warn_on_sensitive_paths: bool = True
    detect_prompt_injection: bool = True
    max_concurrent_reads: Annotated[int, Field(ge=1)] = Field(
        default=4, description="Lower to 1 for old mechanical drives."
    )


class AgentConfig(StrictModel):
    """Guard rails for the tool-call loop."""

    max_tool_iterations: Annotated[int, Field(ge=1)] = 25
    max_turn_seconds: Annotated[float, Field(gt=0)] = 900.0
    max_identical_calls: Annotated[int, Field(ge=1)] = Field(
        default=3, description="Abort if the model repeats one tool call with identical args."
    )
    structured_fallback: bool = Field(
        default=True, description="Emulate tool calling for models without native support."
    )
    auto_compact_at: Annotated[float, Field(gt=0, le=1.0)] = Field(
        default=0.85, description="Fraction of context used before offering compaction."
    )


class UIConfig(StrictModel):
    """Presentation only. No behaviour may depend on these values."""

    theme: Literal["dark", "light", "high-contrast"] = "dark"
    show_thinking: bool = True
    show_token_meter: bool = True
    stream_tool_output: bool = True
    timestamps: bool = False
    keybindings: dict[str, str] = Field(
        default_factory=lambda: {
            "cancel": "escape",
            "emergency_stop": "ctrl+q",
            "command_palette": "ctrl+p",
            "model_selector": "ctrl+m",
            "toggle_mode": "ctrl+g",
            "search_history": "ctrl+f",
            "newline": "shift+enter",
            "help": "f1",
        }
    )


class PrivacyConfig(StrictModel):
    """Privacy posture. Defaults are the most private possible."""

    network_disabled: bool = Field(
        default=False,
        description="Block every tool that would make a network request. Ollama on loopback "
        "is unaffected; a non-loopback Ollama host is blocked.",
    )
    telemetry: Literal[False] = Field(
        default=False,
        description="Structurally impossible to enable. The project ships no telemetry code.",
    )
    warn_on_external_requests: bool = True


class UsageConfig(StrictModel):
    """Usage accounting, including the explicitly-labelled energy estimate."""

    track: bool = True
    estimate_energy: bool = Field(
        default=False, description="Rough Wh estimate. Always labelled an estimate in every view."
    )
    assumed_watts: Annotated[float, Field(gt=0)] = Field(
        default=250.0, description="Whole-system draw assumed while generating. User-calibrated."
    )


class Config(StrictModel):
    """Root configuration object."""

    schema_version: str = CONFIG_SCHEMA_VERSION
    default_model: str | None = None
    default_profile: str | None = None
    system_prompt: str | None = None
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    usage: UsageConfig = Field(default_factory=UsageConfig)
    profiles: dict[str, ModelProfile] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _coherent(self) -> Config:
        if self.default_profile and self.default_profile not in self.profiles:
            raise ValueError(
                f"default_profile {self.default_profile!r} is not defined in [profiles]"
            )
        if self.privacy.network_disabled and not self.ollama.is_loopback:
            raise ValueError(
                f"privacy.network_disabled is set but ollama.host is {self.ollama.host!r}, "
                "which is not loopback. Set ollama.host to 127.0.0.1 or disable network_disabled."
            )
        return self
