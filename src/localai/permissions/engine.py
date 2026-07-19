"""The permissions engine: one decision point for every tool action.

**Invariant.** Every interface -- TUI, CLI, local HTTP API and MCP server -- calls
:meth:`PermissionEngine.evaluate` and honours the result. There is no second path,
no interface-specific shortcut, and no way for a caller to identify itself as
trusted in order to skip a check. ``tests/unit/test_permission_boundaries.py``
asserts this; see docs/permissions-engine.md.

The evaluation order below is deliberate and is part of the security contract. Stages
1-3 are *unconditional*: no rule, mode, grant or configuration value can override
them. Only stages 5 onward consult user policy.

    1. Protected application paths   -> DENY   (cannot be overridden)
    2. Global kill switch            -> DENY   (cannot be overridden)
    3. Read-only mode                -> DENY   (cannot be overridden)
    4. Network policy                -> DENY
    5. Explicit deny rules           -> DENY
    6. Rate limit on high-risk calls -> DENY
    7. Path containment failure      -> DENY or CONFIRM
    8. Sensitive-path classification -> CONFIRM (unless a rule opts in explicitly)
    9. Explicit allow rules + grants -> ALLOW
   10. Permission-mode default       -> ALLOW or CONFIRM
   11. Fallback                      -> CONFIRM (fail closed)
"""

from __future__ import annotations

import fnmatch
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from localai.config.models import (
    Config,
    PermissionMode,
    PermissionRuleModel,
    RiskLevel,
    WorkspaceConfig,
)
from localai.config.paths import AppPaths
from localai.safety import pathsafe, sensitive


class Effect(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


class Interface(StrEnum):
    """Which entry point originated a request. Recorded in every audit entry."""

    TUI = "tui"
    CLI = "cli"
    API = "api"
    MCP = "mcp"


@dataclass(frozen=True, slots=True)
class Caller:
    """Identity of whoever is asking. Never confers authority by itself.

    An external agent connecting over MCP supplies a ``client_id``; that name is
    logged and may select a *more restrictive* profile, but it can never grant
    permissions. See docs/security-model.md#external-agents.
    """

    interface: Interface = Interface.TUI
    client_id: str = "local-user"

    def to_dict(self) -> dict[str, str]:
        return {"interface": self.interface.value, "client_id": self.client_id}


@dataclass(slots=True)
class PermissionRequest:
    """A proposed tool action, fully described before anything executes."""

    tool: str
    risk: RiskLevel
    mutating: bool = False
    network: bool = False
    paths: list[Path] = field(default_factory=list)
    command: str | None = None
    """The exact command line, for shell tools. Matched against command_patterns."""

    arguments: dict[str, Any] = field(default_factory=dict)
    caller: Caller = field(default_factory=Caller)
    cwd: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "risk": self.risk.value,
            "mutating": self.mutating,
            "network": self.network,
            "paths": [str(p) for p in self.paths],
            "command": self.command,
            "arguments": self.arguments,
            "caller": self.caller.to_dict(),
            "cwd": str(self.cwd) if self.cwd else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PermissionRequest:
        """Rebuild from JSON, for ``ai permissions explain --action request.json``."""
        caller_raw = data.get("caller") or {}
        return cls(
            tool=str(data["tool"]),
            risk=RiskLevel(data.get("risk", "read")),
            mutating=bool(data.get("mutating", False)),
            network=bool(data.get("network", False)),
            paths=[Path(p) for p in data.get("paths", [])],
            command=data.get("command"),
            arguments=dict(data.get("arguments") or {}),
            caller=Caller(
                interface=Interface(caller_raw.get("interface", "cli")),
                client_id=str(caller_raw.get("client_id", "local-user")),
            ),
            cwd=Path(data["cwd"]) if data.get("cwd") else None,
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """The engine's verdict, with the full reasoning attached.

    This object is what ``ai permissions explain`` prints, what the confirmation
    prompt renders and what the audit log stores. One shape, three consumers -- so
    what the user is shown is exactly what was decided.
    """

    effect: Effect
    reason: str
    risk: RiskLevel
    stage: str
    """Which numbered stage of the evaluation order produced this verdict."""

    matched_rule_id: str | None = None
    overridable: bool = True
    """False for stages 1-3. A UI must not offer a confirmation prompt for these."""

    workspace: Path | None = None
    sensitive_matches: tuple[sensitive.SensitiveMatch, ...] = ()
    resolved_paths: tuple[pathsafe.ResolvedPath, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def requires_confirmation(self) -> bool:
        return self.effect is Effect.CONFIRM

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect": self.effect.value,
            "decision": self.effect.value,
            "reason": self.reason,
            "risk": self.risk.value,
            "stage": self.stage,
            "matched_rule": self.matched_rule_id,
            "requires_confirmation": self.requires_confirmation,
            "overridable": self.overridable,
            "workspace": str(self.workspace) if self.workspace else None,
            "sensitive": [m.to_dict() for m in self.sensitive_matches],
            "resolved_paths": [
                {
                    "original": r.original,
                    "resolved": str(r.resolved),
                    "exists": r.exists,
                    "inside_workspace": str(r.inside_workspace) if r.inside_workspace else None,
                    "is_reparse_point": r.is_reparse_point,
                    "traversed_reparse_point": (
                        str(r.traversed_reparse_point) if r.traversed_reparse_point else None
                    ),
                }
                for r in self.resolved_paths
            ],
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class SessionGrant:
    """An approval the user gave during this session. Never persisted automatically."""

    tool: str
    scope: str
    """``once`` | ``session`` | ``tool`` | ``command``"""

    command_pattern: str | None = None
    path_prefix: Path | None = None
    used: bool = False
    created_at: float = field(default_factory=time.time)


class PermissionEngine:
    """Evaluates :class:`PermissionRequest` objects against policy.

    Stateless with respect to configuration (re-read on every call, so a mode change
    takes effect immediately) and stateful only for session grants and the
    high-risk rate limiter.
    """

    def __init__(self, config: Config, paths: AppPaths) -> None:
        self._config = config
        self._paths = paths
        self._grants: list[SessionGrant] = []
        self._recent_high_risk: deque[float] = deque(maxlen=512)

    # -- policy surface -------------------------------------------------------

    @property
    def config(self) -> Config:
        return self._config

    def update_config(self, config: Config) -> None:
        """Swap in new configuration. Session grants deliberately survive a mode change."""
        self._config = config

    @property
    def mode(self) -> PermissionMode:
        return self._config.permissions.mode

    def set_mode(self, mode: PermissionMode) -> None:
        self._config.permissions.mode = mode

    def workspaces(self) -> tuple[WorkspaceConfig, ...]:
        return tuple(self._config.permissions.workspaces)

    def workspace_paths(self) -> tuple[Path, ...]:
        return tuple(w.path for w in self._config.permissions.workspaces)

    # -- session grants -------------------------------------------------------

    def grant(
        self,
        tool: str,
        scope: str,
        *,
        command_pattern: str | None = None,
        path_prefix: Path | None = None,
    ) -> SessionGrant:
        """Record an approval. ``scope`` is one of once/session/tool/command."""
        record = SessionGrant(
            tool=tool, scope=scope, command_pattern=command_pattern, path_prefix=path_prefix
        )
        self._grants.append(record)
        return record

    def clear_grants(self) -> int:
        count = len(self._grants)
        self._grants.clear()
        return count

    @property
    def grants(self) -> tuple[SessionGrant, ...]:
        return tuple(self._grants)

    # -- evaluation -----------------------------------------------------------

    def evaluate(self, request: PermissionRequest) -> Decision:
        """Decide whether ``request`` may proceed. The only entry point."""
        perms = self._config.permissions
        safety = self._config.safety
        workspaces = self.workspace_paths()

        resolved = tuple(
            pathsafe.resolve_path(p, workspaces=workspaces, base=request.cwd) for p in request.paths
        )
        workspace = next((r.inside_workspace for r in resolved if r.inside_workspace), None)

        def verdict(effect: Effect, stage: str, reason: str, **kw: Any) -> Decision:
            return Decision(
                effect=effect,
                reason=reason,
                risk=request.risk,
                stage=stage,
                workspace=workspace,
                resolved_paths=resolved,
                **kw,
            )

        # -- Stage 1: protected application paths (unconditional) --------------
        # The model must never be able to rewrite its own permission database or
        # delete the audit log, in any mode, under any rule.
        if request.mutating:
            for r in resolved:
                for protected in self._paths.protected_paths():
                    if r.resolved == protected or pathsafe.is_within(r.resolved, protected):
                        return verdict(
                            Effect.DENY,
                            "protected_path",
                            f"{r.resolved} belongs to localai's own configuration, database or "
                            "audit log. Modifying it through a tool is never permitted, in any "
                            "permission mode. Edit it directly if you intend to change it.",
                            overridable=False,
                        )

        # -- Stage 2: global kill switch (unconditional) -----------------------
        if perms.kill_switch and (request.mutating or request.risk >= RiskLevel.EXECUTE):
            return verdict(
                Effect.DENY,
                "kill_switch",
                "The global kill switch is engaged: every mutating and executing tool is "
                "disabled across all interfaces. Clear it with '/permissions killswitch off'.",
                overridable=False,
            )

        # -- Stage 3: read-only mode (unconditional) ---------------------------
        if safety.read_only and (request.mutating or request.risk >= RiskLevel.WRITE):
            return verdict(
                Effect.DENY,
                "read_only",
                f"Read-only mode is on, so {request.tool!r} (risk: {request.risk.value}) cannot "
                "run. Turn it off with '/mode readonly off'.",
                overridable=False,
            )

        # -- Stage 4: network policy -------------------------------------------
        if request.network and self._config.privacy.network_disabled:
            return verdict(
                Effect.DENY,
                "network_disabled",
                f"{request.tool!r} would make a network request and network-disabled mode is on.",
            )

        # -- Stage 5: explicit deny rules --------------------------------------
        if rule := self._match_rule(request, resolved, effect="deny"):
            return verdict(
                Effect.DENY,
                "deny_rule",
                f"Denied by rule {rule.id!r}" + (f": {rule.note}" if rule.note else "."),
                matched_rule_id=rule.id,
            )

        # -- Stage 6: rate limit on high-risk actions --------------------------
        if request.risk >= RiskLevel.DESTRUCTIVE and (limit := perms.max_high_risk_per_minute):
            now = time.monotonic()
            while self._recent_high_risk and now - self._recent_high_risk[0] > 60.0:
                self._recent_high_risk.popleft()
            if len(self._recent_high_risk) >= limit:
                return verdict(
                    Effect.DENY,
                    "rate_limited",
                    f"{len(self._recent_high_risk)} high-risk actions in the last minute exceeds "
                    f"the limit of {limit}. This guards against a runaway loop. Wait, or raise "
                    "permissions.max_high_risk_per_minute.",
                )

        # -- Stage 7: path containment ------------------------------------------
        warnings: list[str] = []
        for r in resolved:
            if reserved := pathsafe.has_reserved_name(r.resolved):
                return verdict(
                    Effect.DENY,
                    "reserved_device_name",
                    f"{r.original!r} contains the reserved device name {reserved!r}. Opening a "
                    "DOS device can hang the process or touch hardware.",
                )

            if r.traversed_reparse_point and not safety.follow_symlinks:
                if workspaces and r.inside_workspace is None:
                    return verdict(
                        Effect.DENY,
                        "junction_escape",
                        f"{r.original!r} resolves to {r.resolved} by following the junction or "
                        f"symlink at {r.traversed_reparse_point}, which leads outside every "
                        "trusted workspace. Set safety.follow_symlinks = true to permit this.",
                    )
                warnings.append(
                    f"{r.original} is reached through the link at {r.traversed_reparse_point}"
                )

            if (
                str(r.resolved) != str(Path(r.original))
                and r.inside_workspace is None
                and workspaces
            ):
                warnings.append(f"{r.original} resolves outside any workspace, to {r.resolved}")

        # -- Stage 8: sensitive-path classification ------------------------------
        matches: tuple[sensitive.SensitiveMatch, ...] = ()
        if safety.warn_on_sensitive_paths:
            matches = tuple(
                sensitive.classify_paths([r.resolved for r in resolved], mutating=request.mutating)
            )
            if matches:
                opt_in = self._match_rule(request, resolved, effect="allow", require_sensitive=True)
                if opt_in is None:
                    kinds = ", ".join(sorted({m.kind.value for m in matches}))
                    return Decision(
                        effect=Effect.CONFIRM,
                        reason=(
                            f"This touches sensitive data ({kinds}). "
                            + " ".join(f"{m.path.name}: {m.reason}." for m in matches[:3])
                            + " Confirm only if you meant to access this."
                        ),
                        risk=request.risk,
                        stage="sensitive_path",
                        workspace=workspace,
                        sensitive_matches=matches,
                        resolved_paths=resolved,
                        warnings=tuple(warnings),
                    )

        # -- Stage 9: explicit allow rules and session grants --------------------
        if rule := self._match_rule(request, resolved, effect="allow"):
            self._note_high_risk(request)
            return verdict(
                Effect.ALLOW,
                "allow_rule",
                f"Allowed by rule {rule.id!r}" + (f": {rule.note}" if rule.note else "."),
                matched_rule_id=rule.id,
                sensitive_matches=matches,
                warnings=tuple(warnings),
            )

        if grant := self._match_grant(request, resolved):
            if grant.scope == "once":
                grant.used = True
            self._note_high_risk(request)
            return verdict(
                Effect.ALLOW,
                "session_grant",
                f"Approved earlier this session ({grant.scope}).",
                sensitive_matches=matches,
                warnings=tuple(warnings),
            )

        if rule := self._match_rule(request, resolved, effect="confirm"):
            return verdict(
                Effect.CONFIRM,
                "confirm_rule",
                f"Rule {rule.id!r} requires confirmation"
                + (f": {rule.note}" if rule.note else "."),
                matched_rule_id=rule.id,
                sensitive_matches=matches,
                warnings=tuple(warnings),
            )

        # -- Stage 10: permission-mode default -----------------------------------
        effect, reason = self._mode_default(request, workspace, resolved)
        if effect is Effect.ALLOW:
            self._note_high_risk(request)
        return verdict(
            effect,
            "mode_default",
            reason,
            sensitive_matches=matches,
            warnings=tuple(warnings),
        )

    # -- stage helpers --------------------------------------------------------

    def _mode_default(
        self,
        request: PermissionRequest,
        workspace: Path | None,
        resolved: tuple[pathsafe.ResolvedPath, ...],
    ) -> tuple[Effect, str]:
        """Stage 10. Fails closed: anything unrecognised requires confirmation."""
        mode = self.mode
        read_only_action = request.risk is RiskLevel.READ and not request.mutating

        if mode is PermissionMode.BYPASS:
            return Effect.ALLOW, (
                "Bypass mode: no per-action confirmation. Every action is still displayed "
                "and written to the audit log."
            )

        if mode is PermissionMode.MANUAL:
            return Effect.CONFIRM, "Manual mode: every tool action is confirmed."

        if mode is PermissionMode.AUTO:
            if read_only_action:
                if (
                    workspace is None
                    and self._config.permissions.confirm_outside_workspace
                    and self.workspace_paths()
                ):
                    return Effect.CONFIRM, (
                        "Auto mode allows reads, but this path is outside every trusted workspace."
                    )
                return Effect.ALLOW, "Auto mode: read-only actions run without confirmation."
            return Effect.CONFIRM, (
                f"Auto mode confirms {request.risk.value} actions. Only read-only actions "
                "run unattended."
            )

        if mode is PermissionMode.WORKSPACE:
            if workspace is None:
                if not resolved:
                    return Effect.CONFIRM, (
                        "Trusted-workspace mode: this action names no path, so it cannot be "
                        "matched to a workspace."
                    )
                return Effect.CONFIRM, (
                    "Trusted-workspace mode: this path is outside every trusted workspace."
                )
            settings = next(
                (w for w in self.workspaces() if pathsafe.is_within(workspace, w.path)), None
            )
            if settings is None:
                return Effect.CONFIRM, "Trusted-workspace mode: workspace settings not found."
            if request.risk >= RiskLevel.EXECUTE and not settings.allow_execute:
                return Effect.CONFIRM, (
                    f"Workspace {settings.path} does not have allow_execute set, so command "
                    "execution still requires confirmation."
                )
            if request.mutating and not settings.allow_write:
                return Effect.CONFIRM, (
                    f"Workspace {settings.path} is read-only (allow_write = false)."
                )
            return Effect.ALLOW, f"Inside trusted workspace {settings.path}."

        return Effect.CONFIRM, "No policy matched; confirmation is required (fail-closed default)."

    def _note_high_risk(self, request: PermissionRequest) -> None:
        if request.risk >= RiskLevel.DESTRUCTIVE:
            self._recent_high_risk.append(time.monotonic())

    def _match_rule(
        self,
        request: PermissionRequest,
        resolved: tuple[pathsafe.ResolvedPath, ...],
        *,
        effect: str,
        require_sensitive: bool = False,
    ) -> PermissionRuleModel | None:
        """First rule of ``effect`` that matches, in declaration order."""
        now = time.time()
        for rule in self._config.permissions.rules:
            if rule.effect != effect:
                continue
            if rule.expires_at is not None and rule.expires_at < now:
                continue
            if require_sensitive and not rule.allow_sensitive:
                continue
            if rule.interfaces and request.caller.interface.value not in rule.interfaces:
                continue
            if rule.max_risk is not None and request.risk > rule.max_risk:
                continue
            if rule.tools and not _any_match(request.tool, rule.tools):
                continue
            if rule.command_patterns:
                if request.command is None or not _any_match(
                    request.command, rule.command_patterns
                ):
                    continue
            if rule.paths:
                if not resolved:
                    continue
                # Every named path must be covered: a rule that allows one path
                # must not implicitly bless a second, unrelated one in the same call.
                if not all(_any_match(str(r.resolved), rule.paths) for r in resolved):
                    continue
            return rule
        return None

    def _match_grant(
        self, request: PermissionRequest, resolved: tuple[pathsafe.ResolvedPath, ...]
    ) -> SessionGrant | None:
        for grant in self._grants:
            if grant.tool != request.tool or (grant.scope == "once" and grant.used):
                continue
            if grant.command_pattern is not None:
                if request.command is None or not fnmatch.fnmatch(
                    request.command, grant.command_pattern
                ):
                    continue
            if grant.path_prefix is not None:
                if not resolved or not all(
                    pathsafe.is_within(r.resolved, grant.path_prefix) for r in resolved
                ):
                    continue
            return grant
        return None


def _any_match(value: str, patterns: Iterable[str]) -> bool:
    """Case-insensitive fnmatch against any pattern, with separators normalised.

    Windows paths arrive with backslashes but rules are far more readable written
    with forward slashes, so both sides are normalised before matching.
    """
    needle = value.replace("\\", "/").lower()
    return any(fnmatch.fnmatch(needle, p.replace("\\", "/").lower()) for p in patterns)
