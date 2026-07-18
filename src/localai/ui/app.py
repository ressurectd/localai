"""The Textual application.

Presentation and interaction only. Everything else lives behind
:class:`~localai.app.Application`: the agent loop, the permissions engine, storage and
the provider. A widget never evaluates a permission, opens the database or talks to
Ollama.

**Workers matter here.** ``push_screen_wait`` raises ``NoActiveWorker`` unless it is
called from a Textual worker. Every method that opens a modal and waits for an answer
-- including :meth:`_confirm_tool`, which is the permission dialog -- must therefore
run inside an ``@work`` context. ``_run_turn`` is a worker for exactly this reason:
the confirmation prompt is raised from inside it. Removing that decorator silently
breaks the security prompt, so ``tests/integration/test_ui_flows.py`` asserts it.

Layout::

    ◆ model · mode · cwd · context meter                       <- topbar
    ┌──────────────────────────────────────────────────────┐
    │  conversation: you, the model, reasoning, tools       │
    └──────────────────────────────────────────────────────┘
    ◐ thinking  ▂▃▄▅▆▇  3.4s                                  <- thinking indicator
      considering the budget figures...
    ┌ /mo ─────────────────────────────────────────────────┐
    │ ❯ /model    show or set the current model            │  <- command menu
    │   /models   browse and switch models                 │
    └──────────────────────────────────────────────────────┘
    ❯ your prompt                                             <- input
    19 tools · 2 calls · 43 tok/s · local only                <- status bar
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll
from textual.widgets import Static

from localai.agent.loop import AgentEvent, AgentLoop, EventKind
from localai.app import Application
from localai.config.models import PermissionMode, RiskLevel
from localai.domain.messages import Message, Role
from localai.errors import LocalAIError
from localai.permissions.engine import Decision
from localai.providers.base import ModelInfo
from localai.tools.base import Tool
from localai.ui import art
from localai.ui.commands import COMMANDS, CommandResult
from localai.ui.screens import (
    BypassConfirmScreen,
    ConfirmToolScreen,
    HistoryScreen,
    ModelSelectScreen,
    ModeSelectScreen,
    TextScreen,
    ThemeSelectScreen,
)
from localai.ui.widgets import CommandMenu, MenuEntry, PromptArea, ThinkingIndicator, icons


def terminal_supports_unicode() -> bool:
    """Whether the terminal can render box-drawing and block characters.

    Checked rather than assumed: a console stuck on cp1252 renders the sigils as
    mojibake, and a degraded-but-correct interface beats a decorative broken one.
    """
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in encoding or encoding in {"cp65001", ""}


@dataclass
class SessionState:
    """Everything about the current conversation that the UI needs.

    A plain dataclass rather than reactive attributes: the agent loop and the slash
    commands both mutate it, and Textual reactivity across task boundaries is a source
    of subtle bugs. The UI refreshes explicitly after a change.
    """

    model: str | None = None
    model_info: ModelInfo | None = None
    identity: art.ModelIdentity = field(default_factory=lambda: art.UNKNOWN)
    conversation_id: str | None = None
    messages: list[Message] = field(default_factory=list)
    system_prompt: str | None = None
    think: bool | str | None = None
    context_limit: int = 8192
    tokens_used: int = 0
    tokens_exact: bool = False
    last_tps: float | None = None
    tool_calls: int = 0
    busy: bool = False
    started_at: float = field(default_factory=time.time)

    def estimated_tokens(self) -> int:
        return sum(m.approx_tokens() for m in self.messages)


class LocalAIApp(App[None]):
    """The terminal interface."""

    CSS_PATH = "theme.tcss"
    TITLE = "ai"
    SUB_TITLE = "local models, your files"

    BINDINGS: ClassVar[list] = [
        ("ctrl+q", "emergency_stop", "Emergency stop"),
        ("escape", "cancel", "Cancel"),
        ("ctrl+p", "command_palette_open", "Commands"),
        ("ctrl+m", "select_model", "Model"),
        ("ctrl+g", "cycle_mode", "Mode"),
        ("ctrl+o", "select_mode", "Mode picker"),
        ("ctrl+t", "select_theme", "Theme"),
        ("ctrl+f", "search_history", "Search"),
        ("ctrl+l", "clear_view", "Clear"),
        ("f1", "show_help", "Help"),
    ]

    def __init__(
        self,
        core: Application,
        *,
        initial_model: str | None = None,
        resume_id: str | None = None,
        sandbox: bool = False,
    ) -> None:
        super().__init__()
        self.core = core
        self.state = SessionState(model=initial_model or core.config.default_model)
        self.state.system_prompt = core.config.system_prompt
        self._resume_id = resume_id
        self.sandbox = sandbox
        self._cancel = asyncio.Event()
        self._stream_target: Static | None = None
        self._stream_buffer: list[str] = []
        self._thought_seconds = 0.0
        self.unicode_ok = terminal_supports_unicode()
        self.icon = icons(self.unicode_ok)
        # Cached at mount. App.query_one searches the *active* screen, so once a modal
        # is open a query for "#topbar" raises NoMatches -- which previously killed the
        # turn worker in the middle of a confirmation prompt.
        self._topbar: Static | None = None
        self._statusbar: Static | None = None
        self._conversation: VerticalScroll | None = None
        self._thinking: ThinkingIndicator | None = None
        self._menu: CommandMenu | None = None
        self._prompt: PromptArea | None = None

        # The runner calls this whenever policy requires confirmation. Wiring it here
        # is what connects the permissions engine to the modal dialog.
        core.runner.confirm = self._confirm_tool

    # -- cached widget access -------------------------------------------------
    # Each falls back to a query so the accessors work before on_mount finishes.

    @property
    def topbar(self) -> Static:
        return self._topbar or self.query_one("#topbar", Static)

    @property
    def statusbar(self) -> Static:
        return self._statusbar or self.query_one("#statusbar", Static)

    @property
    def conversation(self) -> VerticalScroll:
        return self._conversation or self.query_one("#conversation", VerticalScroll)

    @property
    def thinking(self) -> ThinkingIndicator:
        return self._thinking or self.query_one("#thinking", ThinkingIndicator)

    @property
    def menu(self) -> CommandMenu:
        return self._menu or self.query_one("#command-menu", CommandMenu)

    @property
    def prompt(self) -> PromptArea:
        return self._prompt or self.query_one("#prompt", PromptArea)

    # -- composition ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        yield VerticalScroll(id="conversation")
        yield ThinkingIndicator(unicode_ok=self.unicode_ok, id="thinking")
        yield CommandMenu(unicode_ok=self.unicode_ok, id="command-menu")
        with Container(id="input-area"):
            editor = PromptArea(id="prompt", soft_wrap=True, show_line_numbers=False)
            editor.tab_behavior = "focus"
            yield editor
        yield Static("", id="statusbar")

    async def on_mount(self) -> None:
        self._topbar = self.query_one("#topbar", Static)
        self._statusbar = self.query_one("#statusbar", Static)
        self._conversation = self.conversation
        self._thinking = self.thinking
        self._menu = self.menu
        self._prompt = self.prompt

        for theme in art.custom_themes():
            self.register_theme(theme)  # type: ignore[arg-type]
        configured = self.core.config.ui.theme
        if configured in art.CURATED_THEMES:
            self.theme = configured

        self.prompt.focus()
        self._banner()
        await self._initialise_model()
        if self._resume_id:
            self._resume_conversation(self._resume_id)
        self._refresh_chrome()

    def _banner(self) -> None:
        i = self.icon
        self._write(
            f"[b]ai[/b]  {i['system']}  local models, your files, your machine",
            "msg-system",
        )
        self._write(
            "[dim]F1 help · Ctrl+P commands · Ctrl+M model · Ctrl+O mode · "
            "Ctrl+T theme · Ctrl+Q emergency stop · type / for commands[/dim]",
            "detail",
        )
        if self.sandbox:
            self._write(
                f"{i['warning']}  SANDBOX — mock model, dry-run forced, no real file is touched.",
                "msg-notice",
            )

    async def _initialise_model(self) -> None:
        """Select a model at startup, reporting clearly if none is usable."""
        try:
            models = await self.core.provider.list_models()
        except LocalAIError as exc:
            self._write(f"Cannot reach the model provider: {exc.message}", "msg-error")
            if exc.remediation:
                self._write(f"  {exc.remediation}", "detail")
            return

        if not models:
            self._write(
                "No models are installed. Install one with:  ollama pull qwen3:8b", "msg-error"
            )
            return

        # Honour an explicit choice; otherwise let discovery rank them. Taking the
        # first alphabetically would often land on a model without tool calling and
        # silently degrade the session to the fallback protocol.
        chosen = next((m for m in models if m.name == self.state.model), None)
        if chosen is None:
            from localai.providers.discovery import classify_model

            ranked = sorted(
                (classify_model(m) for m in models),
                key=lambda d: (d.recommended_for_agent, d.info.loaded, d.info.context_length),
                reverse=True,
            )
            chosen = ranked[0].info
        await self._apply_model(chosen, announce=True)

    async def _apply_model(self, model: ModelInfo, *, announce: bool = True) -> None:
        """Adopt a model, including taking on its colour identity."""
        self.state.model = model.name
        self.state.model_info = model
        self.state.context_limit = model.context_length or 8192
        self.state.identity = art.identify(model.name, model.family)

        if announce:
            identity = self.state.identity
            self._write(
                f"[{identity.palette.glow}]{identity.render(unicode_ok=self.unicode_ok)}[/]",
                "sigil",
            )
            capabilities = ", ".join(sorted(model.capabilities)) or "chat"
            title = identity.display or model.name
            tagline = f" — {identity.tagline}" if identity.tagline else ""
            self._write(
                f"[{identity.palette.accent}][b]{model.name}[/b][/]  [dim]{title}{tagline}[/dim]",
                "msg-system",
            )
            self._write(
                f"[dim]  {capabilities}  ·  ctx {self.state.context_limit:,}[/dim]", "detail"
            )
            if not model.supports_tools:
                self._write(
                    f"  {self.icon['warning']} No native tool calling — the structured text "
                    "fallback will be used, which is less reliable.",
                    "msg-notice",
                )

        # The prompt border takes the model's colour, so the whole frame shifts with
        # the model. Guarded because a cosmetic failure must never block a model
        # switch -- but logged rather than swallowed, since a silently missing accent
        # is still a defect, just not one worth aborting for.
        try:
            self.query_one("#input-area").styles.border = (
                "round",
                self.state.identity.palette.accent,
            )
        except Exception:
            self.log.warning("could not apply the model accent colour", exc_info=True)
        self._refresh_chrome()

    # -- chrome ---------------------------------------------------------------

    def _refresh_chrome(self) -> None:
        """Redraw the top and status bars."""
        state = self.state
        config = self.core.config
        mode = self.core.engine.mode
        i = self.icon
        accent = state.identity.palette.accent

        used = state.tokens_used or state.estimated_tokens()
        limit = max(state.context_limit, 1)
        fraction = min(used / limit, 1.0)
        meter_class = (
            "meter-ok" if fraction < 0.7 else ("meter-warn" if fraction < 0.9 else "meter-full")
        )
        filled = int(fraction * 12)
        bar = i["meter_full"] * filled + i["meter_empty"] * (12 - filled)
        exact = "" if state.tokens_exact else "~"

        cwd = str(self.core.cwd)
        if len(cwd) > 38:
            cwd = "…" + cwd[-37:] if self.unicode_ok else "..." + cwd[-35:]

        mode_icon = {
            PermissionMode.MANUAL: i["lock_manual"],
            PermissionMode.AUTO: i["lock_auto"],
            PermissionMode.WORKSPACE: i["lock_workspace"],
            PermissionMode.BYPASS: i["lock_bypass"],
        }[mode]

        self.topbar.update(
            f"[{accent}]{state.identity.icon}[/] "
            f"[b]{state.model or 'no model'}[/b]   "
            f"[{self._mode_markup(mode)}]{mode_icon} {mode.value}[/]   "
            f"[dim]{i['cwd']} {cwd}[/dim]   "
            f"[{meter_class}]{bar}[/] [dim]{exact}{used:,}/{limit:,}[/dim]"
        )

        flags = []
        if config.safety.read_only:
            flags.append("READ-ONLY")
        if config.safety.dry_run:
            flags.append("DRY-RUN")
        if config.permissions.kill_switch:
            flags.append(f"{i['kill']} KILL-SWITCH")
        if config.privacy.network_disabled:
            flags.append("NO-NET")
        if self.sandbox:
            flags.append("SANDBOX")

        throughput = f"{state.last_tps:.0f} tok/s" if state.last_tps else "—"
        flag_text = f"[b yellow]{' '.join(flags)}[/b yellow]" if flags else "[dim]local only[/dim]"
        self.statusbar.update(
            f"[dim]{len(self.core.registry)} tools · {state.tool_calls} calls · "
            f"{throughput}[/dim]  {flag_text}  "
            f"[dim]· Enter send · Shift+Enter newline · / commands[/dim]"
        )

    @staticmethod
    def _mode_markup(mode: PermissionMode) -> str:
        return {
            PermissionMode.MANUAL: "mode-manual",
            PermissionMode.AUTO: "mode-auto",
            PermissionMode.WORKSPACE: "mode-workspace",
            PermissionMode.BYPASS: "mode-bypass",
        }[mode]

    def _write(self, text: str, style: str = "") -> Static:
        """Append a line to the conversation view and scroll to it."""
        widget = Static(text, classes=style, markup=True)
        view = self.conversation
        view.mount(widget)
        view.scroll_end(animate=False)
        return widget

    # -- input and the command menu -------------------------------------------

    @on(PromptArea.TextEdited)
    def _on_prompt_changed(self, event: PromptArea.TextEdited) -> None:
        """Show or filter the slash-command menu as the user types."""
        menu = self.menu
        prompt = self.prompt
        text = event.text

        # Only the first line, and only when it is the whole input, counts as a
        # command: a "/" inside a sentence or on line three is just a character.
        if not text.startswith("/") or "\n" in text:
            menu.hide()
            prompt.menu_open = False
            return

        entries = [
            MenuEntry(name=c.name, summary=c.summary, usage=c.usage)
            for c in COMMANDS.matching(text[1:].split(" ")[0])
        ]
        # Once a full command plus a space is typed, the menu has done its job.
        if " " in text:
            menu.hide()
            prompt.menu_open = False
            return
        menu.show(entries)
        prompt.menu_open = menu.is_open

    @on(PromptArea.NavigateMenu)
    def _on_menu_navigate(self, event: PromptArea.NavigateMenu) -> None:
        if event.direction == "escape":
            self.menu.hide()
            self.prompt.menu_open = False
        elif event.direction == "tab":
            self._complete_from_menu()
        else:
            self.menu.move(event.direction)

    def _complete_from_menu(self) -> None:
        """Replace the input with the highlighted command."""
        menu = self.menu
        selected = menu.selected
        if selected is None:
            return
        prompt = self.prompt
        # A command taking arguments gets a trailing space so you can type straight on.
        prompt.text = f"/{selected.name}" + (" " if selected.usage else "")
        prompt.move_cursor(prompt.document.end)
        menu.hide()
        prompt.menu_open = False

    @on(PromptArea.Submitted)
    async def _on_prompt_submitted(self, event: PromptArea.Submitted) -> None:
        menu = self.menu
        # Enter with the menu open picks the highlighted entry rather than sending
        # a half-typed command.
        if menu.is_open and menu.selected and event.text.strip() != f"/{menu.selected.name}":
            self._complete_from_menu()
            return
        menu.hide()
        self.prompt.menu_open = False
        await self._submit()

    async def _submit(self) -> None:
        prompt = self.prompt
        text = prompt.text.strip()
        if not text:
            return
        if self.state.busy:
            self._write("Still working. Press Esc to cancel first.", "msg-notice")
            return

        prompt.text = ""
        if text.startswith("/"):
            await self._run_command(text)
        else:
            await self._send_message(text)

    async def _run_command(self, line: str) -> None:
        result = COMMANDS.dispatch(self, line)
        if result.message:
            self._write(
                result.message,
                {"error": "msg-error", "warning": "msg-notice", "success": "msg-system"}.get(
                    result.level, "detail"
                ),
            )
        if result.action:
            await self._handle_action(result)
        self._refresh_chrome()

    async def _handle_action(self, result: CommandResult) -> None:
        """Perform a UI action requested by a slash command."""
        action, data = result.action, result.data

        match action:
            case "quit":
                self.exit()
            case "clear":
                await self.conversation.remove_children()
                self.state.messages.clear()
                self._write("Cleared. History is still saved.", "msg-system")
            case "open_models":
                self.action_select_model()
            case "switch_model":
                self._switch_model_worker(data["model"])
            case "open_modes":
                self.action_select_mode()
            case "confirm_bypass":
                self._confirm_bypass()
            case "open_themes":
                self.action_select_theme()
            case "open_history":
                self._open_history()
            case "search_history":
                self._search_history(data["query"])
            case "resume":
                self._resume_conversation(data["conversation_id"])
            case "new_conversation":
                self._new_conversation(data.get("title", ""))
            case "fork":
                self._fork_conversation(data["seq"])
            case "export":
                self._export_conversation(data["format"])
            case "apply_profile":
                self._apply_profile_worker(data["name"])
            case "compact":
                self._compact_context()
            case "doctor":
                self._run_doctor()
            case "open_settings":
                self.push_screen(
                    TextScreen(
                        "Settings",
                        f"Configuration file:\n  {self.core.paths.config_file}\n\n"
                        "Edit it in a text editor, then restart. Validate with:\n"
                        "  localai config validate\n\n"
                        "Runtime toggles:\n"
                        "  /mode                     permission mode picker\n"
                        "  /mode readonly on|off\n"
                        "  /mode dry-run on|off\n"
                        "  /theme                    colour theme picker\n"
                        "  /permissions killswitch on|off",
                    )
                )
            case "show_help":
                await self._run_command("/help")

    # -- conversation ---------------------------------------------------------

    async def _send_message(self, text: str) -> None:
        if not self.state.model:
            self._write("No model selected. Press Ctrl+M to choose one.", "msg-error")
            return

        self._write(f"[b]{self.icon['user']}[/b] {text}", "msg-user")

        if self.state.conversation_id is None:
            record = self.core.conversations.create(
                title=text[:60],
                model=self.state.model,
                system_prompt=self.state.system_prompt,
                workspace=str(self.core.cwd),
            )
            self.state.conversation_id = record.id
            if self.state.system_prompt:
                system = Message(role=Role.SYSTEM, content=self.state.system_prompt)
                self.state.messages.append(system)
                self.core.conversations.add_message(record.id, system)

        message = Message(role=Role.USER, content=text)
        self.state.messages.append(message)
        self.core.conversations.add_message(self.state.conversation_id, message)

        self._cancel = asyncio.Event()
        self._run_turn()

    @work(exclusive=True, group="turn")
    async def _run_turn(self) -> None:
        """Drive one agent turn, rendering events as they arrive.

        **This must remain a worker.** The permission confirmation dialog is raised
        from inside it via ``push_screen_wait``, which raises ``NoActiveWorker``
        outside a worker context. Removing ``@work`` breaks every confirmation
        prompt -- asserted by ``test_confirmation_dialog_appears_in_manual_mode``.
        """
        state = self.state
        state.busy = True
        self._stream_target = None
        self._stream_buffer = []
        thinking = self.thinking
        thinking.start(f"{state.identity.display or 'model'} is thinking")
        self._thought_seconds = 0.0

        context = self.core.tool_context(conversation_id=state.conversation_id, cancel=self._cancel)
        loop = AgentLoop(self.core.provider, self.core.runner, caller=self.core.caller)
        assert state.model is not None

        try:
            async for event in loop.run_turn(
                model=state.model,
                messages=state.messages,
                tools=list(self.core.registry),
                context=context,
                model_info=state.model_info,
                think=state.think,
            ):
                self._render_event(event)
            self._persist_turn(loop.last_result)
        except asyncio.CancelledError:
            self._write("Cancelled.", "msg-notice")
            raise
        except LocalAIError as exc:
            self._write(exc.message, "msg-error")
            if exc.remediation:
                self._write(f"  {exc.remediation}", "detail")
        except Exception as exc:  # never let one turn kill the session
            self.log.error("turn failed", exc_info=True)
            self._write(f"Unexpected error: {type(exc).__name__}: {exc}", "msg-error")
        finally:
            self._thought_seconds += thinking.stop()
            duration = self._thought_seconds
            if duration > 0.05:
                self._write(
                    f"[dim]{self.icon['cwd']} thought for {duration:.1f}s[/dim]", "msg-thinking"
                )
            state.busy = False
            self._refresh_chrome()

    def _render_event(self, event: AgentEvent) -> None:
        """Translate one agent event into terminal output."""
        i = self.icon
        accent = self.state.identity.palette.accent
        thinking = self.thinking

        match event.kind:
            case EventKind.CONTENT_DELTA:
                # First content means reasoning is over: fold the indicator away.
                if self._stream_target is None:
                    self._thought_seconds += thinking.stop()
                    self._stream_target = self._write(
                        f"[{accent}]{i['assistant']}[/] ", "msg-assistant"
                    )
                    self._stream_buffer = []
                self._stream_buffer.append(event.text)
                self._stream_target.update(
                    f"[{accent}]{i['assistant']}[/] " + "".join(self._stream_buffer)
                )
                self.conversation.scroll_end(animate=False)

            case EventKind.THINKING_DELTA:
                if self.core.config.ui.show_thinking:
                    thinking.feed(event.text)

            case EventKind.MESSAGE_COMPLETE:
                self._stream_target = None
                self._stream_buffer = []

            case EventKind.TOOL_REQUESTED:
                # Marked distinctly from model prose: this is a *request*, not an action.
                self._write(f"{i['tool_request']} {event.text}", "tool-request")
                thinking.start(f"running {event.tool_call.name if event.tool_call else 'tool'}")

            case EventKind.TOOL_RESULT:
                self._thought_seconds += thinking.stop()
                self.state.tool_calls += 1
                result = event.tool_result
                if result is None:
                    return
                if not result.ok and "Permission denied" in (result.error or ""):
                    self._write(f"  {i['tool_denied']} DENIED — {result.error}", "tool-denied")
                elif result.ok:
                    self._write(f"  {i['tool_ok']} {self._summarise_result(result)}", "tool-ok")
                else:
                    self._write(f"  {i['tool_fail']} {result.error}", "tool-failed")

                if injection := [f for f in result.flags if f.startswith("injection:")]:
                    self._write(
                        f"  {i['injection']} This content tried to instruct the model "
                        f"({', '.join(injection)}). It is marked as untrusted data.",
                        "injection-warning",
                    )
                if result.truncated and result.full_output_path:
                    self._write(
                        f"    [dim]truncated — full output: {result.full_output_path}[/dim]",
                        "detail",
                    )

            case EventKind.USAGE:
                if event.usage:
                    self.state.tokens_used = (
                        event.usage.prompt_tokens + event.usage.completion_tokens
                    )
                    self.state.tokens_exact = event.usage.token_source.value == "reported"
                    self.state.last_tps = event.usage.tokens_per_second
                    self._refresh_chrome()

            case EventKind.GUARD_TRIPPED:
                self._write(f"{i['warning']} {event.text}", "msg-notice")

            case EventKind.ERROR:
                self._write(f"{i['tool_fail']} {event.text}", "msg-error")
                if remediation := event.detail.get("remediation"):
                    self._write(f"  {remediation}", "detail")

            case EventKind.CANCELLED:
                self._write(event.text or "Cancelled.", "msg-notice")

    @staticmethod
    def _summarise_result(result: Any) -> str:
        """One-line summary of a tool result for the activity display."""
        meta = result.metadata
        for key, label in (
            ("count", "entries"),
            ("matches", "matches"),
            ("returned_lines", "lines"),
            ("exit_code", "exit"),
            ("groups", "duplicate groups"),
            ("added", "+lines"),
        ):
            if key in meta:
                return f"{meta[key]} {label}"
        first = next((line for line in result.content.splitlines() if line.strip()), "")
        return first[:80]

    def _persist_turn(self, turn: Any) -> None:
        """Save messages and usage produced by a completed turn."""
        if turn is None or self.state.conversation_id is None:
            return
        for message in turn.messages:
            self.state.messages.append(message)
            self.core.conversations.add_message(self.state.conversation_id, message)

        if turn.usages and self.core.config.usage.track:
            usage = turn.combined_usage()
            watts = (
                self.core.config.usage.assumed_watts
                if self.core.config.usage.estimate_energy
                else None
            )
            self.core.usage.record(
                usage,
                model=self.state.model or "",
                conversation_id=self.state.conversation_id,
                workspace=str(self.core.cwd),
                tool_calls=turn.tool_calls,
                message_count=len(turn.messages),
                assumed_watts=watts,
            )

        used = self.state.estimated_tokens()
        if used > self.state.context_limit * self.core.config.agent.auto_compact_at:
            self._write(
                f"{self.icon['warning']} Context is {used / self.state.context_limit:.0%} full. "
                "Use /compact to free space, or /new to start fresh.",
                "msg-notice",
            )

    # -- permission confirmation ----------------------------------------------

    async def _confirm_tool(
        self, tool: Tool, arguments: dict[str, Any], decision: Decision
    ) -> bool:
        """Show the confirmation modal and translate the answer into a grant.

        Called by :class:`~localai.tools.runner.ToolRunner` from inside ``_run_turn``,
        which is a worker -- that is what makes ``push_screen_wait`` legal here.
        Returning False is always safe: the tool simply does not run.
        """
        thinking = self.thinking
        thinking.stop()
        danger = "danger-text" if tool.risk >= RiskLevel.DESTRUCTIVE else "msg-notice"
        self._write(f"  {self.icon['lock_manual']} waiting for your decision…", danger)

        choice = await self.push_screen_wait(ConfirmToolScreen(tool, arguments, decision))

        if choice in (None, "deny"):
            return False
        if choice == "session":
            self.core.engine.grant(tool.name, "session")
            self._write(f"    [dim]approved {tool.name} for this session[/dim]", "detail")
        elif choice == "tool":
            self.core.engine.grant(tool.name, "tool")
            self._write(f"    [dim]{tool.name} will not ask again this session[/dim]", "detail")
        elif choice == "command" and (command := arguments.get("command")):
            self.core.engine.grant(tool.name, "command", command_pattern=str(command))
        return True

    @work
    async def _confirm_bypass(self) -> None:
        phrase = self.core.config.permissions.bypass_confirmation_phrase
        if await self.push_screen_wait(BypassConfirmScreen(phrase)):
            self.core.set_mode(PermissionMode.BYPASS)
            self._write(
                f"{self.icon['warning']} BYPASS MODE ENABLED. Every action is still shown "
                "and audited. Ctrl+Q stops everything.",
                "msg-error",
            )
        else:
            self._write("Bypass mode not enabled.", "msg-system")
        self._refresh_chrome()

    # -- actions --------------------------------------------------------------

    def action_emergency_stop(self) -> None:
        """Cancel everything in flight and engage the kill switch.

        Deliberately heavy-handed: this is what someone reaches for when things are
        going wrong, so it stops current work *and* prevents the next mutating action
        until explicitly cleared.
        """
        self._cancel.set()
        self.workers.cancel_group(self, "turn")
        self.core.set_kill_switch(True)
        self.thinking.stop()
        self._write(
            f"{self.icon['kill']} EMERGENCY STOP — generation cancelled and the kill switch "
            "is engaged. No tool can modify anything until you run "
            "'/permissions killswitch off'.",
            "msg-error",
        )
        self.state.busy = False
        self._refresh_chrome()

    def action_cancel(self) -> None:
        menu = self.menu
        if menu.is_open:
            menu.hide()
            self.prompt.menu_open = False
            return
        if self.state.busy:
            self._cancel.set()
            self._write("Cancelling…", "msg-notice")
        else:
            self.prompt.focus()

    @work
    async def action_select_model(self) -> None:
        try:
            models = await self.core.provider.list_models()
        except LocalAIError as exc:
            self._write(exc.message, "msg-error")
            return
        chosen = await self.push_screen_wait(ModelSelectScreen(models, self.state.model))
        if chosen:
            await self._switch_model(chosen)

    @work
    async def _switch_model_worker(self, name: str) -> None:
        await self._switch_model(name)

    async def _switch_model(self, name: str) -> None:
        """Switch models mid-conversation without restarting."""
        try:
            model = await self.core.provider.show_model(name)
        except LocalAIError as exc:
            self._write(exc.message, "msg-error")
            return
        await self._apply_model(model, announce=True)
        if self.state.conversation_id:
            self.core.conversations.set_model(self.state.conversation_id, name)

    @work
    async def action_select_mode(self) -> None:
        chosen = await self.push_screen_wait(ModeSelectScreen(self.core.engine.mode))
        if not chosen:
            return
        mode = PermissionMode(chosen)
        if mode is PermissionMode.BYPASS:
            # Selecting bypass from a list is never enough on its own.
            self._confirm_bypass()
            return
        self.core.set_mode(mode)
        self._write(f"Permission mode: [b]{mode.value}[/b]", "msg-system")
        self._refresh_chrome()

    @work
    async def action_select_theme(self) -> None:
        chosen = await self.push_screen_wait(ThemeSelectScreen(art.CURATED_THEMES, str(self.theme)))
        if not chosen:
            return
        self.theme = chosen
        self.core.config.ui.theme = chosen  # type: ignore[assignment]
        self._write(f"Theme: [b]{chosen}[/b]", "msg-system")

    def action_cycle_mode(self) -> None:
        """Cycle manual → auto → workspace. Bypass is never reachable this way."""
        order = [PermissionMode.MANUAL, PermissionMode.AUTO, PermissionMode.WORKSPACE]
        current = self.core.engine.mode
        nxt = order[(order.index(current) + 1) % len(order)] if current in order else order[0]
        self.core.set_mode(nxt)
        self._write(f"Permission mode: [b]{nxt.value}[/b]", "msg-system")
        self._refresh_chrome()

    def action_search_history(self) -> None:
        prompt = self.prompt
        prompt.text = "/search "
        prompt.move_cursor(prompt.document.end)
        prompt.focus()

    async def action_clear_view(self) -> None:
        await self.conversation.remove_children()

    async def action_show_help(self) -> None:
        result = COMMANDS.dispatch(self, "/help")
        self.push_screen(TextScreen("Help", result.message))

    async def action_command_palette_open(self) -> None:
        """Ctrl+P drops a '/' into the prompt, which opens the live menu."""
        prompt = self.prompt
        prompt.text = "/"
        prompt.move_cursor(prompt.document.end)
        prompt.focus()
        self._on_prompt_changed(PromptArea.TextEdited("/"))

    # -- conversation management ----------------------------------------------

    def _open_history(self) -> None:
        records = self.core.conversations.recent(limit=50)
        rows = [
            {
                "id": r.id,
                "title": r.title or "(untitled)",
                "detail": f"{r.model or '?'} · {r.message_count} messages · "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r.updated_at))}",
            }
            for r in records
        ]
        self.push_screen(HistoryScreen(rows, "Conversations"), self._on_history_chosen)

    def _search_history(self, query: str) -> None:
        hits = self.core.conversations.search(query)
        rows = [
            {
                "id": hit["conversation_id"],
                "title": hit.get("title") or "(untitled)",
                "detail": f"{hit['role']}: {hit['snippet']}",
            }
            for hit in hits
        ]
        self.push_screen(
            HistoryScreen(rows, f"Search: {query!r} ({len(rows)} hits)"),
            self._on_history_chosen,
        )

    def _on_history_chosen(self, conversation_id: str | None) -> None:
        if conversation_id:
            self._resume_conversation(conversation_id)

    @work
    async def _resume_conversation(self, conversation_id: str) -> None:
        record = self.core.conversations.get(conversation_id)
        if record is None:
            self._write(f"No conversation with id {conversation_id}", "msg-error")
            return

        await self.conversation.remove_children()
        self.state.conversation_id = record.id
        self.state.messages = self.core.conversations.messages(record.id)
        self.state.system_prompt = record.system_prompt
        self.state.tool_calls = 0

        i = self.icon
        self._write(f"{i['system']} Resumed: [b]{record.title or record.id}[/b]", "msg-system")
        for message in self.state.messages:
            match message.role:
                case Role.USER:
                    self._write(f"[b]{i['user']}[/b] {message.content}", "msg-user")
                case Role.ASSISTANT:
                    if message.content:
                        self._write(
                            f"[{self.state.identity.palette.accent}]{i['assistant']}[/] "
                            f"{message.content}",
                            "msg-assistant",
                        )
                    for call in message.tool_calls:
                        self._write(f"{i['tool_request']} {call.name}", "tool-request")
                case Role.TOOL:
                    self._write(f"  {i['tool_ok']} {message.name}", "tool-ok")
        if record.model and record.model != self.state.model:
            await self._switch_model(record.model)
        self._refresh_chrome()

    def _new_conversation(self, title: str) -> None:
        self.state.conversation_id = None
        self.state.messages = []
        self.state.tool_calls = 0
        self.state.tokens_used = 0
        self._write(f"New conversation{f': {title}' if title else ''}.", "msg-system")
        self._refresh_chrome()

    def _fork_conversation(self, seq: int) -> None:
        if not self.state.conversation_id:
            self._write("Nothing to fork yet.", "msg-notice")
            return
        try:
            forked = self.core.conversations.fork(self.state.conversation_id, at_seq=seq)
        except KeyError:
            self._write("Could not fork: conversation not found.", "msg-error")
            return
        self._write(f"Forked to {forked.id} at message {seq}.", "msg-system")
        self._resume_conversation(forked.id)

    def _export_conversation(self, fmt: str) -> None:
        if not self.state.conversation_id:
            self._write("Nothing to export yet.", "msg-notice")
            return
        destination = Path.cwd() / f"conversation-{self.state.conversation_id}.{fmt}"
        try:
            written = self.core.conversations.export_to_file(
                self.state.conversation_id, destination, fmt=fmt
            )
        except OSError as exc:
            self._write(f"Export failed: {exc}", "msg-error")
            return
        self._write(f"{self.icon['tool_ok']} Exported to {written}", "msg-system")

    def _compact_context(self) -> None:
        """Drop the middle of the conversation, keeping the system prompt and recent turns.

        Summarising with the model would be better and is planned; this deterministic
        version ships now because it is honest about what it does and cannot
        hallucinate a summary of what was said.
        """
        messages = self.state.messages
        if len(messages) < 8:
            self._write("Not enough history to compact.", "msg-notice")
            return

        head = [m for m in messages[:1] if m.role is Role.SYSTEM]
        tail = messages[-6:]
        dropped = len(messages) - len(head) - len(tail)
        marker = Message(
            role=Role.SYSTEM,
            content=(
                f"[{dropped} earlier message(s) were removed from context to free space. "
                "They remain in the saved conversation and can be searched with /history.]"
            ),
            metadata={"compaction_marker": True},
        )
        self.state.messages = [*head, marker, *tail]
        self._write(
            f"Compacted: {dropped} message(s) removed from context (still saved on disk).",
            "msg-system",
        )
        self._refresh_chrome()

    @work
    async def _apply_profile_worker(self, name: str) -> None:
        profile = self.core.config.profiles.get(name)
        if profile is None:
            return
        self.state.system_prompt = profile.system_prompt or self.state.system_prompt
        self.state.think = profile.think
        await self._switch_model(profile.model)
        self._write(f"Applied profile [b]{name}[/b].", "msg-system")

    @work
    async def _run_doctor(self) -> None:
        from localai.cli import doctor as doctor_module

        checks = await doctor_module.run_checks(self.core)
        marks = {
            "ok": self.icon["tool_ok"],
            "warn": self.icon["warning"],
            "fail": self.icon["tool_fail"],
        }
        body = "\n".join(
            f"{marks.get(c.status, '?')} {c.name:<12} {c.detail}"
            + (f"\n      -> {c.remediation}" if c.remediation else "")
            for c in checks
        )
        self.push_screen(TextScreen("Diagnostics", body))

    async def on_unmount(self) -> None:
        await self.core.aclose()
