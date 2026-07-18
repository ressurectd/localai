"""Permission boundary tests.

These assert the security contract in docs/permissions-engine.md. They are marked
``security`` and must never be skipped or weakened to make another change pass. If
one of these fails, the failure is the finding.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from localai.config.models import (
    Config,
    PermissionMode,
    PermissionRuleModel,
    RiskLevel,
)
from localai.config.paths import AppPaths
from localai.permissions.engine import (
    Caller,
    Effect,
    Interface,
    PermissionEngine,
    PermissionRequest,
)

pytestmark = pytest.mark.security


def request_for(
    tool: str = "read_file",
    *,
    risk: RiskLevel = RiskLevel.READ,
    mutating: bool = False,
    paths: list[Path] | None = None,
    command: str | None = None,
    interface: Interface = Interface.TUI,
    client_id: str = "local-user",
    network: bool = False,
) -> PermissionRequest:
    return PermissionRequest(
        tool=tool,
        risk=risk,
        mutating=mutating,
        network=network,
        paths=paths or [],
        command=command,
        caller=Caller(interface=interface, client_id=client_id),
    )


# --- Stage 1: protected application paths ----------------------------------


@pytest.mark.parametrize("target", ["config_file", "database", "audit_log"])
def test_model_cannot_modify_its_own_permission_or_audit_state(
    engine: PermissionEngine, paths: AppPaths, target: str
) -> None:
    """The model must never be able to rewrite policy or erase the audit trail."""
    decision = engine.evaluate(
        request_for(
            "write_file", risk=RiskLevel.WRITE, mutating=True, paths=[getattr(paths, target)]
        )
    )
    assert decision.effect is Effect.DENY
    assert decision.stage == "protected_path"
    assert decision.overridable is False


def test_protected_path_denial_survives_bypass_mode(
    engine: PermissionEngine, paths: AppPaths
) -> None:
    """Bypass mode grants autonomy, not the ability to disable oversight."""
    engine.set_mode(PermissionMode.BYPASS)
    decision = engine.evaluate(
        request_for(
            "delete_path", risk=RiskLevel.DESTRUCTIVE, mutating=True, paths=[paths.audit_log]
        )
    )
    assert decision.effect is Effect.DENY
    assert decision.overridable is False


def test_protected_path_denial_survives_an_explicit_allow_rule(
    config: Config, paths: AppPaths
) -> None:
    """A permissive rule cannot re-open a stage-1 denial."""
    config.permissions.rules.append(
        PermissionRuleModel(
            id="allow-all-writes", effect="allow", tools=["write_file"], paths=["**"]
        )
    )
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(
        request_for("write_file", risk=RiskLevel.WRITE, mutating=True, paths=[paths.database])
    )
    assert decision.effect is Effect.DENY
    assert decision.stage == "protected_path"


def test_reading_a_protected_path_is_allowed(engine: PermissionEngine, paths: AppPaths) -> None:
    """Only *mutation* is blocked. Inspecting your own audit log is legitimate."""
    decision = engine.evaluate(request_for("read_file", paths=[paths.audit_log]))
    assert decision.effect is not Effect.DENY


# --- Stage 2 & 3: kill switch and read-only --------------------------------


def test_kill_switch_blocks_mutation_everywhere(config: Config, paths: AppPaths) -> None:
    config.permissions.kill_switch = True
    config.permissions.mode = PermissionMode.BYPASS
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(
        request_for("run_powershell", risk=RiskLevel.EXECUTE, mutating=True, command="dir")
    )
    assert decision.effect is Effect.DENY
    assert decision.stage == "kill_switch"
    assert decision.overridable is False


def test_kill_switch_still_permits_reads(config: Config, paths: AppPaths) -> None:
    """A kill switch stops damage; it should not make the tool useless."""
    config.permissions.kill_switch = True
    engine = PermissionEngine(config, paths)
    assert engine.evaluate(request_for("list_directory")).effect is not Effect.DENY


def test_read_only_mode_denies_every_mutating_tool(config: Config, paths: AppPaths) -> None:
    config.safety.read_only = True
    config.permissions.mode = PermissionMode.BYPASS
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(
        request_for("write_file", risk=RiskLevel.WRITE, mutating=True, paths=[Path("x.txt")])
    )
    assert decision.effect is Effect.DENY
    assert decision.stage == "read_only"
    assert decision.overridable is False


# --- External agents gain nothing from their identity -----------------------


@pytest.mark.parametrize("client", ["claude-code", "codex", "cursor", "opencode", "localai"])
def test_external_agent_identity_confers_no_authority(
    engine: PermissionEngine, workspace: Path, client: str
) -> None:
    """A caller cannot become trusted by naming itself.

    This is the concrete test for "never infer bypass permission from the identity
    of Claude Code, Codex or another client".
    """
    engine.set_mode(PermissionMode.MANUAL)
    decision = engine.evaluate(
        request_for(
            "run_powershell",
            risk=RiskLevel.EXECUTE,
            mutating=True,
            command="Remove-Item x -Recurse -Force",
            interface=Interface.MCP,
            client_id=client,
        )
    )
    assert decision.effect is not Effect.ALLOW


def test_every_interface_gets_the_same_verdict(engine: PermissionEngine, workspace: Path) -> None:
    """There is one policy, not one per entry point."""
    verdicts = {
        interface: engine.evaluate(
            request_for(
                "delete_path",
                risk=RiskLevel.DESTRUCTIVE,
                mutating=True,
                paths=[workspace / "readme.txt"],
                interface=interface,
            )
        ).effect
        for interface in Interface
    }
    assert len(set(verdicts.values())) == 1, f"interfaces disagreed: {verdicts}"


# --- Mode semantics ---------------------------------------------------------


def test_manual_mode_confirms_even_a_read(config: Config, paths: AppPaths, workspace: Path) -> None:
    config.permissions.mode = PermissionMode.MANUAL
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(request_for("read_file", paths=[workspace / "readme.txt"]))
    assert decision.effect is Effect.CONFIRM


def test_auto_mode_allows_reads_but_confirms_writes(
    engine: PermissionEngine, workspace: Path
) -> None:
    assert (
        engine.evaluate(request_for("read_file", paths=[workspace / "readme.txt"])).effect
        is Effect.ALLOW
    )
    assert (
        engine.evaluate(
            request_for(
                "write_file", risk=RiskLevel.WRITE, mutating=True, paths=[workspace / "new.txt"]
            )
        ).effect
        is Effect.CONFIRM
    )


def test_workspace_mode_allows_inside_and_confirms_outside(
    config: Config, paths: AppPaths, workspace: Path, tmp_path: Path
) -> None:
    config.permissions.mode = PermissionMode.WORKSPACE
    engine = PermissionEngine(config, paths)

    inside = engine.evaluate(
        request_for(
            "write_file", risk=RiskLevel.WRITE, mutating=True, paths=[workspace / "docs" / "new.md"]
        )
    )
    assert inside.effect is Effect.ALLOW
    assert inside.workspace is not None

    outside = engine.evaluate(
        request_for(
            "write_file", risk=RiskLevel.WRITE, mutating=True, paths=[tmp_path / "elsewhere.txt"]
        )
    )
    assert outside.effect is Effect.CONFIRM


def test_workspace_mode_still_confirms_execution_without_allow_execute(
    config: Config, paths: AppPaths, workspace: Path
) -> None:
    """allow_write does not imply allow_execute."""
    config.permissions.mode = PermissionMode.WORKSPACE
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(
        request_for(
            "run_powershell",
            risk=RiskLevel.EXECUTE,
            mutating=True,
            paths=[workspace],
            command="Get-ChildItem",
        )
    )
    assert decision.effect is Effect.CONFIRM


def test_bypass_mode_allows_without_confirmation(
    config: Config, paths: AppPaths, workspace: Path
) -> None:
    config.permissions.mode = PermissionMode.BYPASS
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(
        request_for("write_file", risk=RiskLevel.WRITE, mutating=True, paths=[workspace / "x.txt"])
    )
    assert decision.effect is Effect.ALLOW


def test_unknown_situation_fails_closed(config: Config, paths: AppPaths) -> None:
    """With no workspaces and a path-free execute request, the answer is CONFIRM."""
    config.permissions.mode = PermissionMode.WORKSPACE
    config.permissions.workspaces = []
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(
        request_for("run_powershell", risk=RiskLevel.EXECUTE, mutating=True, command="dir")
    )
    assert decision.effect is Effect.CONFIRM


# --- Sensitive paths --------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "C:/Users/x/AppData/Local/Google/Chrome/User Data/Default/Cookies",
        "C:/Users/x/.ssh/id_rsa",
        "C:/Users/x/.aws/credentials",
        "C:/Users/x/vault.kdbx",
        "C:/Users/x/project/.env",
    ],
)
def test_sensitive_paths_require_confirmation_even_in_auto_mode(
    engine: PermissionEngine, path: str
) -> None:
    """Auto mode allows reads, but not silent reads of credential material."""
    decision = engine.evaluate(request_for("read_file", paths=[Path(path)]))
    assert decision.effect is Effect.CONFIRM
    assert decision.stage == "sensitive_path"
    assert decision.sensitive_matches


def test_ordinary_allow_rule_does_not_cover_sensitive_paths(
    config: Config, paths: AppPaths
) -> None:
    """Opting in to sensitive data requires ``allow_sensitive``, set deliberately."""
    config.permissions.rules.append(
        PermissionRuleModel(id="reads", effect="allow", tools=["read_file"], paths=["**"])
    )
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(request_for("read_file", paths=[Path("C:/Users/x/.ssh/id_rsa")]))
    assert decision.effect is Effect.CONFIRM


def test_allow_sensitive_rule_permits_it(config: Config, paths: AppPaths) -> None:
    config.permissions.rules.append(
        PermissionRuleModel(
            id="ssh-audit",
            effect="allow",
            tools=["read_file"],
            paths=["*/.ssh/*"],
            allow_sensitive=True,
            note="reviewing my own keys",
        )
    )
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(request_for("read_file", paths=[Path("C:/Users/x/.ssh/id_rsa")]))
    assert decision.effect is Effect.ALLOW
    assert decision.matched_rule_id == "ssh-audit"


# --- Rules ------------------------------------------------------------------


def test_deny_rule_beats_allow_rule(config: Config, paths: AppPaths, workspace: Path) -> None:
    """Deny is evaluated at stage 5, allow at stage 9. Order is the guarantee."""
    config.permissions.rules += [
        PermissionRuleModel(id="allow-writes", effect="allow", tools=["write_file"], paths=["**"]),
        PermissionRuleModel(id="deny-secrets", effect="deny", paths=["**/secret*"]),
    ]
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(
        request_for(
            "write_file", risk=RiskLevel.WRITE, mutating=True, paths=[workspace / "secrets.txt"]
        )
    )
    assert decision.effect is Effect.DENY
    assert decision.matched_rule_id == "deny-secrets"


def test_command_pattern_rule_matches(config: Config, paths: AppPaths) -> None:
    config.permissions.rules.append(
        PermissionRuleModel(
            id="git-status",
            effect="allow",
            tools=["run_powershell"],
            command_patterns=["git status*"],
        )
    )
    engine = PermissionEngine(config, paths)
    assert (
        engine.evaluate(
            request_for(
                "run_powershell",
                risk=RiskLevel.EXECUTE,
                mutating=True,
                command="git status --short",
            )
        ).effect
        is Effect.ALLOW
    )
    assert (
        engine.evaluate(
            request_for(
                "run_powershell", risk=RiskLevel.EXECUTE, mutating=True, command="git push --force"
            )
        ).effect
        is not Effect.ALLOW
    )


def test_expired_rule_is_ignored(config: Config, paths: AppPaths, workspace: Path) -> None:
    config.permissions.rules.append(
        PermissionRuleModel(
            id="temporary",
            effect="allow",
            tools=["write_file"],
            paths=["**"],
            expires_at=time.time() - 60,
        )
    )
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(
        request_for("write_file", risk=RiskLevel.WRITE, mutating=True, paths=[workspace / "x.txt"])
    )
    assert decision.matched_rule_id != "temporary"


def test_allow_rule_must_cover_every_path_in_the_request(
    config: Config, paths: AppPaths, workspace: Path, tmp_path: Path
) -> None:
    """A rule allowing one path must not bless an unrelated second path in the same call."""
    config.permissions.rules.append(
        PermissionRuleModel(
            id="ws-only",
            effect="allow",
            tools=["move_path"],
            paths=[str(workspace).replace("\\", "/") + "/**"],
        )
    )
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(
        request_for(
            "move_path",
            risk=RiskLevel.WRITE,
            mutating=True,
            paths=[workspace / "readme.txt", tmp_path / "outside.txt"],
        )
    )
    assert decision.matched_rule_id != "ws-only"


def test_allow_rule_with_no_selectors_is_rejected_at_config_time() -> None:
    """A blanket allow rule is a configuration error, not a runtime surprise."""
    with pytest.raises(ValueError, match="must constrain at least one"):
        PermissionRuleModel(id="everything", effect="allow")


# --- Session grants ---------------------------------------------------------


def test_approve_once_is_consumed(engine: PermissionEngine, workspace: Path) -> None:
    engine.set_mode(PermissionMode.MANUAL)
    engine.grant("read_file", "once")
    first = engine.evaluate(request_for("read_file", paths=[workspace / "readme.txt"]))
    second = engine.evaluate(request_for("read_file", paths=[workspace / "readme.txt"]))
    assert first.effect is Effect.ALLOW
    assert second.effect is Effect.CONFIRM


def test_session_grant_persists_across_calls(engine: PermissionEngine, workspace: Path) -> None:
    engine.set_mode(PermissionMode.MANUAL)
    engine.grant("read_file", "session")
    for _ in range(3):
        assert (
            engine.evaluate(request_for("read_file", paths=[workspace / "readme.txt"])).effect
            is Effect.ALLOW
        )


def test_grant_is_scoped_to_its_tool(engine: PermissionEngine, workspace: Path) -> None:
    engine.set_mode(PermissionMode.MANUAL)
    engine.grant("read_file", "session")
    decision = engine.evaluate(
        request_for(
            "delete_path",
            risk=RiskLevel.DESTRUCTIVE,
            mutating=True,
            paths=[workspace / "readme.txt"],
        )
    )
    assert decision.effect is Effect.CONFIRM


def test_clearing_grants_restores_confirmation(engine: PermissionEngine, workspace: Path) -> None:
    engine.set_mode(PermissionMode.MANUAL)
    engine.grant("read_file", "session")
    assert engine.clear_grants() == 1
    assert (
        engine.evaluate(request_for("read_file", paths=[workspace / "readme.txt"])).effect
        is Effect.CONFIRM
    )


# --- Rate limiting and network ---------------------------------------------


def test_high_risk_actions_are_rate_limited(
    config: Config, paths: AppPaths, workspace: Path
) -> None:
    """A runaway loop of destructive calls is stopped even in bypass mode."""
    config.permissions.mode = PermissionMode.BYPASS
    config.permissions.max_high_risk_per_minute = 3
    engine = PermissionEngine(config, paths)

    effects = [
        engine.evaluate(
            request_for(
                "delete_path",
                risk=RiskLevel.DESTRUCTIVE,
                mutating=True,
                paths=[workspace / f"f{i}.txt"],
            )
        ).effect
        for i in range(5)
    ]
    assert effects[:3] == [Effect.ALLOW] * 3
    assert Effect.DENY in effects[3:]


def test_network_disabled_blocks_networked_tools(config: Config, paths: AppPaths) -> None:
    config.privacy.network_disabled = True
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(request_for("fetch_url", network=True))
    assert decision.effect is Effect.DENY
    assert decision.stage == "network_disabled"


# --- Decision explainability ------------------------------------------------


def test_decision_serialises_everything_explain_needs(
    engine: PermissionEngine, workspace: Path
) -> None:
    """``localai permissions explain`` renders exactly these fields."""
    decision = engine.evaluate(request_for("read_file", paths=[workspace / "readme.txt"]))
    data = decision.to_dict()
    for field in (
        "decision",
        "reason",
        "risk",
        "stage",
        "matched_rule",
        "requires_confirmation",
        "overridable",
        "workspace",
        "sensitive",
        "resolved_paths",
        "warnings",
    ):
        assert field in data, f"explain output is missing {field!r}"


def test_request_round_trips_through_json(workspace: Path) -> None:
    """``--action request.json`` must reconstruct the same request."""
    original = request_for(
        "run_powershell",
        risk=RiskLevel.EXECUTE,
        mutating=True,
        paths=[workspace],
        command="Get-ChildItem",
        interface=Interface.MCP,
        client_id="codex",
    )
    restored = PermissionRequest.from_dict(original.to_dict())
    assert restored.tool == original.tool
    assert restored.risk == original.risk
    assert restored.command == original.command
    assert restored.caller.client_id == "codex"
    assert restored.caller.interface is Interface.MCP
