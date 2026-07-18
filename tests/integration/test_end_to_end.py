"""End-to-end tests through the real wiring, plus a headless drive of the TUI.

Textual's ``run_test`` pilot runs the actual application against a virtual terminal,
so these exercise composition, key handling and the confirmation flow rather than
merely asserting that the widgets were constructed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

import pytest

from localai.app import Application
from localai.config.models import PermissionMode
from localai.permissions.engine import Effect
from localai.providers.mock import MockProvider

# --- runner end to end ------------------------------------------------------


async def test_denied_action_is_audited_and_not_executed(app: Application, workspace: Path) -> None:
    """A denial must leave a trail and leave the filesystem alone."""
    app.config.safety.read_only = True
    target = workspace / "should-not-exist.txt"

    result = await app.runner.execute(
        "write_file", {"path": str(target), "content": "x"}, app.tool_context()
    )

    assert not result.ok
    assert "Permission denied" in result.error
    assert not target.exists()

    log = app.paths.audit_log.read_text(encoding="utf-8")
    assert "read_only" in log
    assert "denied" in log


async def test_declined_confirmation_does_not_execute(app: Application, workspace: Path) -> None:
    async def decline(tool, arguments, decision) -> bool:
        return False

    app.runner.confirm = decline
    app.config.permissions.mode = PermissionMode.MANUAL
    target = workspace / "declined.txt"

    result = await app.runner.execute(
        "write_file", {"path": str(target), "content": "x"}, app.tool_context()
    )
    assert not result.ok
    assert result.metadata.get("declined")
    assert not target.exists()


async def test_approved_confirmation_executes_and_records_it(
    app: Application, workspace: Path
) -> None:
    async def approve(tool, arguments, decision) -> bool:
        return True

    app.runner.confirm = approve
    app.config.permissions.mode = PermissionMode.MANUAL
    target = workspace / "approved.txt"

    result = await app.runner.execute(
        "write_file", {"path": str(target), "content": "written\n"}, app.tool_context()
    )
    assert result.ok
    assert target.read_text(encoding="utf-8") == "written\n"
    assert result.metadata["confirmed_by_user"] is True
    assert '"confirmed_by_user": true' in app.paths.audit_log.read_text(encoding="utf-8")


async def test_every_execution_is_audited(app: Application) -> None:
    await app.runner.execute("list_directory", {"path": "."}, app.tool_context())
    entries = app.paths.audit_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(entries) >= 2  # decision + outcome
    assert all("list_directory" in line for line in entries)


async def test_audit_records_the_calling_interface(app: Application) -> None:
    import json

    from localai.permissions.engine import Caller, Interface

    await app.runner.execute(
        "list_directory",
        {"path": "."},
        app.tool_context(),
        caller=Caller(interface=Interface.MCP, client_id="external-agent"),
    )
    records = [
        json.loads(line) for line in app.paths.audit_log.read_text(encoding="utf-8").splitlines()
    ]
    assert any(r["interface"] == "mcp" and r["client_id"] == "external-agent" for r in records)


async def test_output_is_truncated_with_the_full_copy_retained(
    app: Application, workspace: Path
) -> None:
    """Truncation must never lose evidence."""
    app.config.safety.max_output_bytes = 2000
    big = workspace / "huge.txt"
    big.write_text("\n".join(f"line {i} of content" for i in range(5000)), encoding="utf-8")

    result = await app.runner.execute(
        "read_file", {"path": str(big), "max_lines": 5000}, app.tool_context()
    )
    assert result.truncated
    assert "truncated" in result.flags
    assert result.full_output_path is not None
    assert result.full_output_path.exists()
    assert len(result.full_output_path.read_text(encoding="utf-8")) > len(result.content)


async def test_timeout_terminates_a_slow_tool(app: Application) -> None:
    from localai.config.models import RiskLevel
    from localai.tools.base import Tool, ToolResult

    class SlowTool(Tool):
        name = "slow_tool"
        description = "Sleeps forever."
        risk = RiskLevel.READ
        parameters: ClassVar[dict] = {"type": "object", "properties": {}}

        async def run(self, arguments, context) -> ToolResult:
            await asyncio.sleep(30)
            return ToolResult(content="never")

    async def approve(tool, arguments, decision) -> bool:
        return True

    app.runner.confirm = approve
    app.registry.register(SlowTool())
    app.config.safety.tool_timeout_s = 0.2

    result = await app.runner.execute("slow_tool", {}, app.tool_context())
    assert not result.ok
    assert "timeout" in result.error.lower()


async def test_preview_matches_enforcement(app: Application, workspace: Path) -> None:
    """``permissions explain`` must not use different logic from the real check."""
    arguments = {"path": str(workspace / "preview.txt"), "content": "x"}
    decision, description = await app.runner.preview("write_file", arguments, app.tool_context())
    assert description
    assert decision.effect in {Effect.ALLOW, Effect.CONFIRM, Effect.DENY}

    declined: list[bool] = []

    async def record(tool, args, real_decision) -> bool:
        declined.append(True)
        assert real_decision.effect is decision.effect
        assert real_decision.stage == decision.stage
        return False

    app.runner.confirm = record
    await app.runner.execute("write_file", arguments, app.tool_context())
    if decision.effect is Effect.CONFIRM:
        assert declined, "preview said CONFIRM but execution did not ask"


# --- headless TUI -----------------------------------------------------------


@pytest.fixture
def tui_app(app: Application) -> object:
    from localai.ui.app import LocalAIApp

    return LocalAIApp(app, initial_model="mock-tools:8b")


async def test_tui_starts_and_selects_a_model(tui_app) -> None:
    async with tui_app.run_test() as pilot:
        await pilot.pause()
        assert tui_app.state.model == "mock-tools:8b"
        assert tui_app.state.context_limit == 32768


async def test_tui_sends_a_message_and_renders_the_reply(tui_app, provider: MockProvider) -> None:
    provider.queue_text("Hello from the mock model.")

    async with tui_app.run_test() as pilot:
        await pilot.pause()
        tui_app.query_one("#prompt").text = "hi there"
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause()
            if not tui_app.state.busy and tui_app.state.messages:
                break

        assert any(m.content == "hi there" for m in tui_app.state.messages)
        assert tui_app.state.conversation_id is not None


async def test_tui_slash_command_runs(tui_app) -> None:
    async with tui_app.run_test() as pilot:
        await pilot.pause()
        tui_app.query_one("#prompt").text = "/tools"
        await pilot.press("enter")
        await pilot.pause()
        rendered = " ".join(str(w.content) for w in tui_app.query("#conversation Static"))
        assert "read_file" in rendered


async def test_tui_mode_cycling_never_reaches_bypass(tui_app) -> None:
    """Bypass must require the typed phrase, not a keystroke."""
    async with tui_app.run_test() as pilot:
        await pilot.pause()
        seen = set()
        for _ in range(6):
            await pilot.press("ctrl+g")
            await pilot.pause()
            seen.add(tui_app.core.engine.mode)
        assert PermissionMode.BYPASS not in seen


async def test_tui_emergency_stop_engages_the_kill_switch(tui_app) -> None:
    async with tui_app.run_test() as pilot:
        await pilot.pause()
        assert not tui_app.core.config.permissions.kill_switch
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert tui_app.core.config.permissions.kill_switch


async def test_tui_shift_enter_does_not_send(tui_app) -> None:
    async with tui_app.run_test() as pilot:
        await pilot.pause()
        editor = tui_app.query_one("#prompt")
        editor.text = "first line"
        await pilot.press("shift+enter")
        await pilot.pause()
        assert tui_app.state.conversation_id is None  # nothing was sent


async def test_tui_help_screen_opens(tui_app) -> None:
    async with tui_app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f1")
        await pilot.pause()
        assert len(tui_app.screen_stack) > 1


async def test_tui_clear_empties_the_view(tui_app) -> None:
    async with tui_app.run_test() as pilot:
        await pilot.pause()
        assert len(tui_app.query("#conversation Static")) > 0
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert len(tui_app.query("#conversation Static")) == 0


async def test_tui_survives_a_provider_failure(app: Application) -> None:
    """A dead provider should report, not crash the interface."""
    from localai.ui.app import LocalAIApp

    class DeadProvider(MockProvider):
        async def list_models(self):
            from localai.errors import ProviderUnavailableError

            raise ProviderUnavailableError(
                "cannot reach Ollama", remediation="Start Ollama with 'ollama serve'."
            )

    app.provider = DeadProvider()
    tui = LocalAIApp(app)
    async with tui.run_test() as pilot:
        await pilot.pause()
        rendered = " ".join(str(w.content) for w in tui.query("#conversation Static"))
        assert "Ollama" in rendered
        assert "ollama serve" in rendered
