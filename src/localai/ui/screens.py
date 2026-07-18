"""Modal screens: confirmation, model selection, permission mode, history, help.

The confirmation screen is the security-critical one. It shows the exact command or
path, the risk classification, why the engine is asking, and any sensitive-data or
injection warnings -- then offers approval scopes. It never summarises a command it is
asking the user to authorise.

All of these are pushed with ``push_screen_wait``, which **requires a Textual worker
context**. Callers must be decorated with ``@work``; see ui/app.py.
"""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from localai.config.models import PermissionMode, RiskLevel
from localai.permissions.engine import Decision
from localai.providers.base import ModelInfo
from localai.tools.base import Tool
from localai.ui import art
from localai.ui.widgets import ICONS_UNICODE as I

#: What the user chose in the confirmation dialog.
#: ``once`` | ``session`` | ``tool`` | ``command`` | ``deny``
ConfirmChoice = str


class ConfirmToolScreen(ModalScreen[ConfirmChoice]):
    """Ask the user to authorise one tool call."""

    BINDINGS: ClassVar[list] = [
        ("escape", "deny", "Deny"),
        ("y", "approve_once", "Approve once"),
        ("a", "approve_session", "Approve for session"),
        ("t", "approve_tool", "Always allow tool"),
    ]

    def __init__(self, tool: Tool, arguments: dict[str, Any], decision: Decision) -> None:
        super().__init__()
        self.tool = tool
        self.arguments = arguments
        self.decision = decision

    def compose(self) -> ComposeResult:
        destructive = self.tool.risk >= RiskLevel.DESTRUCTIVE
        badge = I["warning"] if destructive else I["lock_manual"]
        with Vertical(id="dialog", classes="danger" if destructive else ""):
            yield Label(
                f"{badge}  "
                + ("DESTRUCTIVE ACTION" if destructive else "Permission required")
                + f"   ·   {self.tool.name}",
                id="dialog-title",
                classes="danger-text" if destructive else "",
            )
            with VerticalScroll(id="dialog-body"):
                # The exact action, verbatim. Never abbreviated for this prompt.
                yield Static(f"[b]{self.tool.describe_call(self.arguments)}[/b]")
                yield Static(
                    f"\n  risk       {self.decision.risk.value}"
                    f"\n  stage      {self.decision.stage}"
                    f"\n  workspace  {self.decision.workspace or 'outside every workspace'}",
                    classes="detail",
                )
                yield Static(f"\n{self.decision.reason}", classes="detail")

                for match in self.decision.sensitive_matches:
                    yield Static(
                        f"\n{I['warning']}  SENSITIVE: {match.path.name} — {match.reason}",
                        classes="danger-text",
                    )
                for warning in self.decision.warnings:
                    yield Static(f"\n{I['warning']}  {warning}", classes="msg-notice")
                if self.tool.mutating:
                    backups = self.app.core.config.safety.backup_before_modify  # type: ignore[attr-defined]
                    yield Static(
                        "\nThis modifies your computer."
                        + (" A backup will be taken first." if backups else " Backups are OFF."),
                        classes="msg-notice",
                    )
            with Horizontal(id="dialog-buttons"):
                yield Button(f"{I['tool_denied']} Deny  Esc", variant="error", id="deny")
                yield Button(f"{I['tool_ok']} Once  Y", variant="primary", id="once")
                yield Button("Session  A", id="session")
                yield Button("Always  T", id="tool")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "deny")

    def action_deny(self) -> None:
        self.dismiss("deny")

    def action_approve_once(self) -> None:
        self.dismiss("once")

    def action_approve_session(self) -> None:
        self.dismiss("session")

    def action_approve_tool(self) -> None:
        self.dismiss("tool")


class BypassConfirmScreen(ModalScreen[bool]):
    """Require the exact configured phrase before enabling bypass mode.

    A yes/no button would be too easy to click through. Typing the phrase makes the
    decision deliberate, which is the entire point of the mode existing.
    """

    def __init__(self, phrase: str) -> None:
        super().__init__()
        self.phrase = phrase

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="danger"):
            yield Label(
                f"{I['warning']}  Enable BYPASS mode?", id="dialog-title", classes="danger-text"
            )
            with VerticalScroll(id="dialog-body"):
                yield Static(
                    "In bypass mode the model runs tools without asking you first. It can "
                    "create, modify and delete files, and run PowerShell commands, on its "
                    "own initiative.\n\n"
                    "These protections remain in force and cannot be disabled:\n"
                    f"  {I['tool_ok']} every action is displayed as it happens\n"
                    f"  {I['tool_ok']} every action is written to the audit log\n"
                    f"  {I['tool_ok']} localai's own config, database and log stay unwritable\n"
                    f"  {I['tool_ok']} Ctrl+Q stops everything immediately\n"
                    f"  {I['tool_ok']} deletions still go to the Recycle Bin\n\n"
                    "Do not use bypass mode with a model acting on documents you do not "
                    "trust.\n",
                    classes="detail",
                )
                yield Static(f"Type exactly:  [b]{self.phrase}[/b]", classes="danger-text")
                yield Input(placeholder=self.phrase, id="phrase")
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel  Esc", variant="primary", id="cancel")
                yield Button(f"{I['warning']} Enable bypass", variant="error", id="confirm")

    def on_mount(self) -> None:
        self.query_one("#phrase", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._check(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self._check(self.query_one("#phrase", Input).value)
        else:
            self.dismiss(False)

    def _check(self, typed: str) -> None:
        # Exact match after stripping surrounding whitespace only. Case must match.
        self.dismiss(typed.strip() == self.phrase)

    def key_escape(self) -> None:
        self.dismiss(False)


class ModeSelectScreen(ModalScreen[str | None]):
    """Pick a permission mode, with the consequences spelled out.

    Bypass is listed but selecting it only *requests* it -- the app then runs the
    phrase-confirmation screen. A single list selection must never be enough to
    disable confirmation.
    """

    BINDINGS: ClassVar[list] = [("escape", "cancel", "Cancel")]

    DESCRIPTIONS: ClassVar[dict[PermissionMode, tuple[str, str, str]]] = {
        PermissionMode.MANUAL: (
            "lock_manual",
            "Confirm everything",
            "Every action asks first, including reading a file. Slowest, safest.",
        ),
        PermissionMode.AUTO: (
            "lock_auto",
            "Confirm changes",
            "Reads run freely. Writes, deletes and commands ask. Sensible default.",
        ),
        PermissionMode.WORKSPACE: (
            "lock_workspace",
            "Trust a workspace",
            "Free rein inside directories you have trusted; asks everywhere else.",
        ),
        PermissionMode.BYPASS: (
            "lock_bypass",
            "No confirmation",
            "Runs anything without asking. Still displayed and audited. Needs a phrase.",
        ),
    }

    def __init__(self, current: PermissionMode) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"{I['lock_auto']}  Permission mode", id="dialog-title")
            options: list[Option] = []
            for mode, (icon_key, headline, detail) in self.DESCRIPTIONS.items():
                active = "  ← current" if mode is self.current else ""
                danger = "   [DANGER]" if mode is PermissionMode.BYPASS else ""
                options.append(
                    Option(
                        f"{I[icon_key]}  [b]{mode.value}[/b]{active}{danger}\n"
                        f"      {headline}\n"
                        f"      [dim]{detail}[/dim]",
                        id=mode.value,
                    )
                )
            yield OptionList(*options, id="mode-list")
            yield Static(
                "Enter to select · Esc to cancel · Ctrl+G cycles the safe modes",
                classes="detail",
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ModelSelectScreen(ModalScreen[str | None]):
    """Pick a model, showing each family's colour and how well it is supported."""

    BINDINGS: ClassVar[list] = [("escape", "cancel", "Cancel")]

    def __init__(self, models: list[ModelInfo], current: str | None) -> None:
        super().__init__()
        self.models = models
        self.current = current

    def compose(self) -> ComposeResult:
        from localai.providers.discovery import SupportLevel, classify_model

        with Vertical(id="dialog"):
            yield Label(f"{I['model']}  Select a model", id="dialog-title")
            options: list[Option] = []
            # Best-suited first: full tool support, then already loaded, then by name.
            ranked = sorted(
                (classify_model(m) for m in self.models),
                key=lambda d: (not d.recommended_for_agent, not d.info.loaded, d.name),
            )
            for entry in ranked:
                model = entry.info
                identity = art.identify(model.name, model.family)
                support = {
                    SupportLevel.FULL: "[green]full tools[/green]",
                    SupportLevel.FALLBACK: "[yellow]text fallback[/yellow]",
                    SupportLevel.CHAT_ONLY: "[dim]chat only[/dim]",
                    SupportLevel.EMBEDDING: "[blue]embedding[/blue]",
                }[entry.support]
                memory = model.estimated_memory_bytes() / 1024**3
                estimate = "" if model.vram_bytes else "~"
                current = "  ← current" if model.name == self.current else ""
                loaded = f"  [green]{I['tool_ok']} loaded[/green]" if model.loaded else ""
                extras = " ".join(c for c in ("thinking", "vision") if c in model.capabilities)
                context = f"{model.context_length:,}" if model.context_length else "?"
                options.append(
                    Option(
                        f"[{identity.palette.accent}]{identity.icon}[/] "
                        f"[b]{model.name}[/b]{current}{loaded}\n"
                        f"      {support}  ·  {model.parameter_size or '?'} "
                        f"{model.quantization or ''}  ·  ctx {context}  ·  "
                        f"{estimate}{memory:.1f} GB" + (f"  ·  {extras}" if extras else ""),
                        id=model.name,
                    )
                )
            yield OptionList(*options, id="model-list")
            yield Static(
                f"{I['tool_ok']} loaded · ~ estimated memory · Enter select · Esc cancel",
                classes="detail",
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ThemeSelectScreen(ModalScreen[str | None]):
    """Pick a colour theme."""

    BINDINGS: ClassVar[list] = [("escape", "cancel", "Cancel")]

    def __init__(self, themes: dict[str, str], current: str) -> None:
        super().__init__()
        self.themes = themes
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Colour theme", id="dialog-title")
            yield OptionList(
                *[
                    Option(
                        f"[b]{name}[/b]{'  ← current' if name == self.current else ''}\n"
                        f"      [dim]{description}[/dim]",
                        id=name,
                    )
                    for name, description in self.themes.items()
                ],
                id="theme-list",
            )
            yield Static("Enter to apply · Esc to cancel", classes="detail")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class HistoryScreen(ModalScreen[str | None]):
    """Browse saved conversations, or search results."""

    BINDINGS: ClassVar[list] = [("escape", "cancel", "Cancel")]

    def __init__(self, rows: list[dict[str, Any]], title: str = "Conversations") -> None:
        super().__init__()
        self.rows = rows
        self.title_text = title

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"{I['search']}  {self.title_text}", id="dialog-title")
            if not self.rows:
                yield Static("Nothing found.", classes="detail")
            else:
                yield OptionList(
                    *[
                        Option(
                            f"[b]{row.get('title') or '(untitled)'}[/b]\n"
                            f"      [dim]{row.get('detail', '')}[/dim]",
                            id=str(row["id"]),
                        )
                        for row in self.rows
                    ],
                    id="history-list",
                )
            yield Static("Enter to open · Esc to cancel", classes="detail")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class TextScreen(ModalScreen[None]):
    """Scrollable read-only text: help, diagnostics, context inspection."""

    BINDINGS: ClassVar[list] = [
        ("escape", "dismiss_screen", "Close"),
        ("q", "dismiss_screen", "Close"),
    ]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, id="dialog-title")
            with VerticalScroll(id="dialog-body"):
                yield Static(self.body)
            yield Static("Esc to close", classes="detail")

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)
