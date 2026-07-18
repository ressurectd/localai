"""Golden tests for the CLI's machine-readable contracts.

External agents branch on these shapes, so a change here is a breaking change that
needs a version bump and a CHANGELOG entry. These tests are what turn that from a
convention into something enforced.

Every command is invoked in-process through ``main()`` with a temporary home and the
mock provider, so the suite never touches a real Ollama daemon or the user's data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from localai.cli.main import main
from localai.version import CLI_SCHEMA_VERSION


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    """Invoke the CLI and capture stdout/stderr separately."""
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict]:
    """Invoke with --json and parse stdout, asserting the log/data separation."""
    code, out, _ = run(capsys, *args, "--json", "--mock")
    return code, json.loads(out)


# --- the core contract ------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "schema"),
    [
        (["version"], "localai.version/1"),
        (["doctor"], "localai.doctor/1"),
        (["capabilities"], "localai.capabilities/1"),
        (["architecture"], "localai.architecture/1"),
        (["models", "list"], "localai.models/1"),
        (["tools", "list"], "localai.tools/1"),
        (["config", "validate"], "localai.config-validate/1"),
        (["config", "path"], "localai.config-paths/1"),
        (["permissions", "show"], "localai.permissions/1"),
        (["permissions", "validate"], "localai.permissions-validate/1"),
        (["usage", "report"], "localai.usage-report/1"),
        (["migrations", "status"], "localai.migrations/1"),
        (["history", "list"], "localai.history/1"),
        (["logs"], "localai.audit/1"),
    ],
)
def test_every_json_command_declares_its_schema(
    capsys: pytest.CaptureFixture[str], args: list[str], schema: str
) -> None:
    _, payload = run_json(capsys, *args)
    assert payload["schema"] == schema
    assert payload["schema_version"] == CLI_SCHEMA_VERSION


@pytest.mark.parametrize(
    "args",
    [
        ["version"],
        ["doctor"],
        ["capabilities"],
        ["models", "list"],
        ["tools", "list"],
        ["usage", "report"],
        ["logs"],
    ],
)
def test_stdout_is_pure_json(capsys: pytest.CaptureFixture[str], args: list[str]) -> None:
    """``localai ... --json | jq`` must never break on a stray log line."""
    _, out, _ = run(capsys, *args, "--json", "--mock", "--verbose")
    json.loads(out)  # raises if anything non-JSON reached stdout


def test_logs_go_to_stderr_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(capsys, "doctor", "--json", "--mock", "--verbose")
    assert json.loads(out)  # parseable despite verbose logging


# --- specific shapes --------------------------------------------------------


def test_version_lists_every_interface(capsys: pytest.CaptureFixture[str]) -> None:
    _, payload = run_json(capsys, "version")
    for interface in (
        "app",
        "cli_schema",
        "config_schema",
        "tool_api",
        "permission_schema",
        "plugin_manifest",
        "local_api",
    ):
        assert interface in payload["versions"]


def test_doctor_reports_status_per_check(capsys: pytest.CaptureFixture[str]) -> None:
    _, payload = run_json(capsys, "doctor")
    assert isinstance(payload["ok"], bool)
    assert payload["summary"].keys() >= {"ok", "warn", "fail"}
    for check in payload["checks"]:
        assert check["status"] in {"ok", "warn", "fail"}
        assert check["name"]
        if check["status"] != "ok":
            assert "remediation" in check


def test_doctor_exit_code_reflects_failures(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = run_json(capsys, "doctor")
    assert code == (0 if payload["ok"] else 1)


def test_capabilities_distinguishes_built_from_planned(capsys: pytest.CaptureFixture[str]) -> None:
    """An agent must be able to tell what exists from what is planned."""
    _, payload = run_json(capsys, "capabilities")
    features = payload["features"]
    assert features["permission_engine"] is True
    assert features["audit_log"] is True
    assert features["streaming_chat"] is True
    # Honest about unbuilt phases rather than claiming them.
    assert features["mcp_server"] is False
    assert features["local_http_api"] is False
    assert features["fine_tuning"] is False


def test_tools_list_shape(capsys: pytest.CaptureFixture[str]) -> None:
    _, payload = run_json(capsys, "tools", "list")
    assert len(payload["tools"]) >= 19
    for tool in payload["tools"]:
        assert tool.keys() >= {
            "name",
            "description",
            "risk",
            "mutating",
            "network",
            "parameters",
            "category",
        }
        assert tool["risk"] in {"read", "write", "destructive", "execute", "privileged"}
        assert tool["parameters"]["type"] == "object"


def test_tool_schema_command_returns_the_schema(capsys: pytest.CaptureFixture[str]) -> None:
    _, payload = run_json(capsys, "tools", "schema", "read_file")
    assert payload["tool"] == "read_file"
    assert "path" in payload["parameters"]["properties"]
    assert "path" in payload["parameters"]["required"]


def test_unknown_tool_exits_with_noinput(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        run(capsys, "tools", "describe", "no_such_tool", "--json", "--mock")
    assert info.value.code == 66  # EX_NOINPUT


def test_usage_report_declares_token_exactness(capsys: pytest.CaptureFixture[str]) -> None:
    """The honesty requirement, enforced at the contract level."""
    _, payload = run_json(capsys, "usage", "report", "--period", "all")
    totals = payload["totals"]
    assert totals["token_exactness"] in {"exact", "mixed", "estimated", "unknown", "empty"}
    assert totals["energy_is_estimate"] is True
    assert totals["generations_by_source"].keys() == {"reported", "estimated", "unknown"}
    assert any("estimate" in note for note in payload["notes"])


def test_architecture_lists_invariants(capsys: pytest.CaptureFixture[str]) -> None:
    _, payload = run_json(capsys, "architecture")
    assert payload["composition_root"] == "localai.app.Application.create"
    assert len(payload["invariants"]) >= 5
    assert "permissions" in payload["layers"]
    assert payload["layers"]["permissions"]["security_sensitive"]


def test_migrations_status_shape(capsys: pytest.CaptureFixture[str]) -> None:
    _, payload = run_json(capsys, "migrations", "status")
    assert payload["current_version"] == payload["latest_version"]
    assert payload["pending"] == []


def test_models_list_uses_the_mock_provider(capsys: pytest.CaptureFixture[str]) -> None:
    _, payload = run_json(capsys, "models", "list")
    names = {m["name"] for m in payload["models"]}
    assert "mock-tools:8b" in names
    for model in payload["models"]:
        assert "supports_tools" in model
        assert "estimated_memory_is_estimate" in model


# --- permissions explain ----------------------------------------------------


def test_permissions_explain_returns_full_reasoning(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The command must show every field the spec calls for."""
    action = tmp_path / "action.json"
    action.write_text(
        json.dumps(
            {
                "tool": "delete_path",
                "risk": "destructive",
                "mutating": True,
                "paths": [str(tmp_path / "target.txt")],
                "caller": {"interface": "mcp", "client_id": "external-agent"},
            }
        ),
        encoding="utf-8",
    )
    _, out, _ = run(capsys, "permissions", "explain", "--action", str(action), "--json", "--mock")
    payload = json.loads(out)

    decision = payload["decision"]
    for field in (
        "decision",
        "reason",
        "risk",
        "stage",
        "matched_rule",
        "requires_confirmation",
        "workspace",
        "overridable",
    ):
        assert field in decision, f"explain is missing {field}"
    assert payload["request"]["tool"] == "delete_path"
    assert decision["decision"] in {"allow", "confirm", "deny"}


def test_permissions_explain_denies_a_protected_path(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, isolated_home: Path
) -> None:
    from localai.config.paths import AppPaths

    action = tmp_path / "action.json"
    action.write_text(
        json.dumps(
            {
                "tool": "write_file",
                "risk": "write",
                "mutating": True,
                "paths": [str(AppPaths.resolve(isolated_home).audit_log)],
            }
        ),
        encoding="utf-8",
    )
    # A deny is a successful *explanation*, so the command returns rather than
    # raising; the exit code is what signals the verdict to a caller.
    code, out, _ = run(
        capsys, "permissions", "explain", "--action", str(action), "--json", "--mock"
    )
    assert code == 77  # EX_NOPERM

    decision = json.loads(out)["decision"]
    assert decision["decision"] == "deny"
    assert decision["stage"] == "protected_path"
    assert decision["overridable"] is False


# --- chat -------------------------------------------------------------------


def test_chat_returns_usage_with_provenance(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(
        capsys, "chat", "--model", "mock-tools:8b", "--prompt", "hello", "--json", "--mock"
    )
    payload = json.loads(out)
    assert code == 0
    assert payload["schema"] == "localai.chat/1"
    assert payload["model"] == "mock-tools:8b"
    assert payload["usage"]["token_source"] in {"reported", "estimated", "unknown"}
    assert "thinking_token_source" in payload["usage"]


def test_chat_streams_ndjson(capsys: pytest.CaptureFixture[str]) -> None:
    """Each line must be independently parseable, for line-by-line consumers."""
    code, out, _ = run(
        capsys,
        "chat",
        "--model",
        "mock-tools:8b",
        "--prompt",
        "hi",
        "--json",
        "--stream",
        "--mock",
    )
    lines = [line for line in out.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]
    kinds = [e["event"] for e in events]
    assert "turn_start" in kinds
    assert "turn_complete" in kinds
    assert code == 0


def test_chat_with_an_unknown_model_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        run(capsys, "chat", "--model", "nope:1b", "--prompt", "hi", "--json", "--mock")
    assert info.value.code == 66


def test_error_envelope_shape(capsys: pytest.CaptureFixture[str]) -> None:
    """Failures are machine-readable too."""
    with pytest.raises(SystemExit):
        run(capsys, "chat", "--model", "nope:1b", "--prompt", "hi", "--json", "--mock")
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"].keys() >= {"code", "message", "remediation"}
    assert payload["error"]["code"] == "model_not_found"


# --- test-tool --------------------------------------------------------------


def test_test_tool_runs_a_read(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("hello from the fixture\n", encoding="utf-8")
    args_file = tmp_path / "args.json"
    args_file.write_text(json.dumps({"path": str(target)}), encoding="utf-8")

    code, out, _ = run(
        capsys,
        "test-tool",
        "read_file",
        "--args-file",
        str(args_file),
        "--json",
        "--mock",
        "--yes",
        "--cwd",
        str(tmp_path),
    )
    payload = json.loads(out)
    assert code == 0
    assert payload["result"]["ok"]
    assert "hello from the fixture" in payload["result"]["content"]
    assert payload["decision"]["decision"] in {"allow", "confirm"}


def test_test_tool_accepts_inline_args(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, out, _ = run(
        capsys,
        "test-tool",
        "list_directory",
        "--args",
        json.dumps({"path": str(tmp_path)}),
        "--json",
        "--mock",
        "--yes",
        "--cwd",
        str(tmp_path),
    )
    assert json.loads(out)["result"]["ok"]
    assert code == 0


def test_test_tool_declines_without_yes(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """A non-interactive caller must not be treated as having approved."""
    code, out, _ = run(
        capsys,
        "test-tool",
        "write_file",
        "--args",
        json.dumps({"path": str(tmp_path / "new.txt"), "content": "x"}),
        "--json",
        "--mock",
        "--cwd",
        str(tmp_path),
    )
    payload = json.loads(out)
    assert code == 1
    assert not payload["result"]["ok"]
    assert not (tmp_path / "new.txt").exists()


# --- output modes -----------------------------------------------------------


def test_no_color_produces_no_ansi(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(capsys, "tools", "list", "--no-color", "--mock")
    assert "\033[" not in out


def test_quiet_suppresses_informational_output(capsys: pytest.CaptureFixture[str]) -> None:
    _, quiet_out, _ = run(capsys, "config", "path", "--quiet", "--mock")
    _, normal_out, _ = run(capsys, "config", "path", "--mock")
    assert len(quiet_out) <= len(normal_out)


def test_deterministic_output_across_runs(capsys: pytest.CaptureFixture[str]) -> None:
    """Same input, same output -- required for golden-file comparison by agents."""
    _, first = run_json(capsys, "tools", "list")
    _, second = run_json(capsys, "tools", "list")
    assert first["tools"] == second["tools"]
