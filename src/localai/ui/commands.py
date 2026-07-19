"""Slash commands.

Commands are declared as data and dispatched through a registry, so the help text,
the command palette and the parser cannot disagree with each other. Each handler is
a plain function taking ``(app, args)`` and returning a :class:`CommandResult` --
no Textual types appear here, which keeps the whole surface unit-testable without a
terminal.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from localai.ui.app import LocalAIApp


@dataclass(slots=True)
class CommandResult:
    """What a command did, for the UI to render."""

    message: str = ""
    level: str = "info"
    """``info`` | ``success`` | ``warning`` | ``error``"""

    action: str = ""
    """A UI action the app should take: ``quit``, ``clear``, ``open_models``, ..."""

    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SlashCommand:
    name: str
    summary: str
    handler: Callable[[LocalAIApp, list[str]], CommandResult]
    usage: str = ""
    aliases: tuple[str, ...] = ()


class CommandRegistry:
    """Holds every slash command."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        self._aliases: dict[str, str] = {}

    def add(self, command: SlashCommand) -> SlashCommand:
        self._commands[command.name] = command
        for alias in command.aliases:
            self._aliases[alias] = command.name
        return command

    def get(self, name: str) -> SlashCommand | None:
        key = name.lstrip("/").lower()
        return self._commands.get(self._aliases.get(key, key))

    def all(self) -> list[SlashCommand]:
        return sorted(self._commands.values(), key=lambda c: c.name)

    def matching(self, prefix: str) -> list[SlashCommand]:
        """Commands whose name starts with ``prefix``, for palette completion."""
        needle = prefix.lstrip("/").lower()
        return [c for c in self.all() if c.name.startswith(needle)]

    def dispatch(self, app: LocalAIApp, line: str) -> CommandResult:
        """Parse and run a slash command line."""
        text = line.strip()
        if not text.startswith("/"):
            return CommandResult(f"not a command: {text!r}", "error")

        try:
            parts = shlex.split(text[1:], posix=False)
        except ValueError:
            parts = text[1:].split()
        if not parts:
            return CommandResult("empty command", "error")

        name, arguments = parts[0], [p.strip('"') for p in parts[1:]]
        command = self.get(name)
        if command is None:
            suggestions = [c.name for c in self.matching(name)][:3]
            hint = (
                f" Did you mean: {', '.join('/' + s for s in suggestions)}?" if suggestions else ""
            )
            return CommandResult(f"unknown command /{name}.{hint} Try /help.", "error")

        try:
            return command.handler(app, arguments)
        except Exception as exc:  # a bad command must never take the UI down
            return CommandResult(f"/{name} failed: {type(exc).__name__}: {exc}", "error")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _help(app: LocalAIApp, args: list[str]) -> CommandResult:
    """The full key and command reference.

    This lives here rather than on screen at startup: a wall of shortcuts is noise
    you stop reading after the first session, and it costs the space the conversation
    should have. The status bar carries a single "/help" pointer instead.
    """
    if args:
        command = COMMANDS.get(args[0])
        if command is None:
            close = ", ".join("/" + c.name for c in COMMANDS.matching(args[0])[:3])
            hint = f"  Did you mean: {close}?" if close else ""
            return CommandResult(f"unknown command /{args[0]}.{hint}", "error")
        usage = f"\n\n  usage:  {command.usage}" if command.usage else ""
        return CommandResult(f"[b]/{command.name}[/b]  —  {command.summary}{usage}")

    keys = [
        ("Enter", "send"),
        ("Shift+Enter", "new line"),
        ("/", "command menu — type to filter, Tab completes"),
        ("Esc", "cancel generation, or dismiss the menu"),
        ("Ctrl+C ×2", "exit"),
        ("Ctrl+Q", "emergency stop — cancels and locks all tools"),
        ("Ctrl+P", "command menu"),
        ("Ctrl+M", "model picker"),
        ("Ctrl+O", "permission mode picker"),
        ("Ctrl+T", "colour theme"),
        ("Ctrl+G", "cycle permission mode"),
        ("Ctrl+F", "search history"),
        ("Ctrl+L", "clear the view"),
        ("F1", "this help"),
    ]
    width = max(len(k) for k, _ in keys)
    lines = ["[b]Keys[/b]", ""]
    lines += [f"  [b]{key:<{width}}[/b]   [dim]{what}[/dim]" for key, what in keys]

    commands = COMMANDS.all()
    name_width = max(len(c.name) for c in commands) + 1
    lines += ["", "[b]Commands[/b]", ""]
    lines += [f"  [b]/{c.name:<{name_width}}[/b] [dim]{c.summary}[/dim]" for c in commands]
    lines += [
        "",
        "[dim]/help <command> for detail on one.[/dim]",
    ]
    return CommandResult("\n".join(lines), action="show_help_screen")


def _models(app: LocalAIApp, args: list[str]) -> CommandResult:
    return CommandResult(action="open_models")


def _model(app: LocalAIApp, args: list[str]) -> CommandResult:
    if not args:
        # No argument means "let me choose" -- a list beats making the user recall a
        # tag they may not remember exactly.
        return CommandResult(action="open_models")
    return CommandResult(action="switch_model", data={"model": args[0]})


def _mode(app: LocalAIApp, args: list[str]) -> CommandResult:
    from localai.config.models import PermissionMode

    if not args:
        # A list with consequences spelled out beats making the user recall four
        # mode names and what each one permits.
        return CommandResult(action="open_modes")

    requested = args[0].lower()
    # Sub-toggles that are safety flags rather than permission modes.
    if requested in {"readonly", "read-only", "dry-run", "dryrun", "network"}:
        return _safety_toggle(app, requested, args[1:])

    try:
        mode = PermissionMode(requested)
    except ValueError:
        return CommandResult(
            f"unknown mode {requested!r}. Choose from: "
            + ", ".join(m.value for m in PermissionMode),
            "error",
        )

    if mode is PermissionMode.BYPASS:
        # Bypass is never entered from a one-word command. The confirmation screen
        # requires the exact phrase from config, typed verbatim.
        return CommandResult(action="confirm_bypass")

    app.core.set_mode(mode)
    return CommandResult(f"Permission mode is now {mode.value}.", "success")


def _safety_toggle(app: LocalAIApp, flag: str, args: list[str]) -> CommandResult:
    """Toggle a safety switch, e.g. ``/mode readonly on``."""
    safety = app.core.config.safety
    privacy = app.core.config.privacy
    wanted = args[0].lower() in {"on", "true", "yes", "1"} if args else None

    if flag in {"readonly", "read-only"}:
        safety.read_only = (not safety.read_only) if wanted is None else wanted
        return CommandResult(
            f"Read-only mode {'ON: no tool can modify anything' if safety.read_only else 'off'}.",
            "success" if safety.read_only else "info",
        )
    if flag in {"dry-run", "dryrun"}:
        safety.dry_run = (not safety.dry_run) if wanted is None else wanted
        return CommandResult(
            f"Dry-run {'ON: mutations are simulated' if safety.dry_run else 'off'}.",
            "success" if safety.dry_run else "info",
        )
    # /mode network on  ->  network *enabled*, i.e. network_disabled = False
    privacy.network_disabled = (not privacy.network_disabled) if wanted is None else not wanted
    return CommandResult(
        f"Network access {'disabled' if privacy.network_disabled else 'enabled'}.",
        "success",
    )


def _theme(app: LocalAIApp, args: list[str]) -> CommandResult:
    from localai.ui import art

    if not args:
        return CommandResult(action="open_themes")
    if args[0] not in art.CURATED_THEMES:
        available = ", ".join(art.CURATED_THEMES)
        return CommandResult(f"unknown theme {args[0]!r}. Available: {available}", "error")
    return CommandResult(action="open_themes", data={"theme": args[0]})


def _permissions(app: LocalAIApp, args: list[str]) -> CommandResult:
    if args and args[0] == "killswitch":
        engage = args[1].lower() in {"on", "true", "yes"} if len(args) > 1 else True
        app.core.set_kill_switch(engage)
        return CommandResult(
            "KILL SWITCH ENGAGED: all mutating and executing tools are disabled."
            if engage
            else "Kill switch cleared.",
            "warning" if engage else "success",
        )
    if args and args[0] == "clear":
        count = app.core.engine.clear_grants()
        return CommandResult(f"Cleared {count} session approval(s).", "success")

    perms = app.core.config.permissions
    safety = app.core.config.safety
    lines = [
        f"Mode:          {perms.mode.value}",
        f"Kill switch:   {'ENGAGED' if perms.kill_switch else 'off'}",
        f"Read-only:     {safety.read_only}",
        f"Dry-run:       {safety.dry_run}",
        f"Recycle bin:   {safety.use_recycle_bin}",
        f"Backups:       {safety.backup_before_modify}",
        f"Session grants:{len(app.core.engine.grants)}",
        "",
        f"Trusted workspaces ({len(perms.workspaces)}):",
    ]
    lines += [
        f"  {w.path}  ({'write' if w.allow_write else 'read'}"
        f"{', execute' if w.allow_execute else ''})"
        for w in perms.workspaces
    ] or ["  (none)"]
    lines += ["", f"Rules ({len(perms.rules)}):"]
    lines += [f"  [{r.effect}] {r.id}: {r.note or '-'}" for r in perms.rules] or ["  (none)"]
    lines += ["", "/permissions killswitch on|off   /permissions clear"]
    return CommandResult("\n".join(lines))


def _workspace(app: LocalAIApp, args: list[str]) -> CommandResult:
    from pathlib import Path

    from localai.config.models import WorkspaceConfig

    if not args:
        spaces = app.core.config.permissions.workspaces
        listing = "\n".join(f"  {w.path}" for w in spaces) or "  (none)"
        return CommandResult(f"Working directory: {app.core.cwd}\nTrusted workspaces:\n{listing}")

    if args[0] == "add":
        if len(args) < 2:
            return CommandResult("usage: /workspace add <path>", "error")
        path = Path(args[1]).expanduser().resolve()
        if not path.is_dir():
            return CommandResult(f"{path} is not a directory", "error")
        app.core.config.permissions.workspaces.append(WorkspaceConfig(path=path, name=path.name))
        app.core.audit.record_event(
            "workspace_added", app.core.caller, reason=str(path), path=str(path)
        )
        return CommandResult(f"Trusted workspace added: {path}", "success")

    if args[0] == "remove":
        if len(args) < 2:
            return CommandResult("usage: /workspace remove <path>", "error")
        target = Path(args[1]).expanduser().resolve()
        before = len(app.core.config.permissions.workspaces)
        app.core.config.permissions.workspaces = [
            w for w in app.core.config.permissions.workspaces if w.path != target
        ]
        removed = before - len(app.core.config.permissions.workspaces)
        return CommandResult(
            f"Removed {removed} workspace(s).", "success" if removed else "warning"
        )

    path = Path(args[0]).expanduser().resolve()
    if not path.is_dir():
        return CommandResult(f"{path} is not a directory", "error")
    app.core.cwd = path
    return CommandResult(f"Working directory: {path}", "success")


def _tools(app: LocalAIApp, args: list[str]) -> CommandResult:
    if args:
        try:
            tool = app.core.registry.get(args[0])
        except Exception as exc:
            return CommandResult(str(exc), "error")
        required = set(tool.parameters.get("required", []))
        params = "\n".join(
            f"    {'*' if k in required else ' '} {k}: {v.get('type', 'any')} "
            f"- {v.get('description', '')}"
            for k, v in tool.parameters.get("properties", {}).items()
        )
        return CommandResult(
            f"{tool.name}  [{tool.risk.value}"
            f"{', mutating' if tool.mutating else ''}]\n{tool.description}\n\nParameters:\n{params}"
        )

    by_category: dict[str, list[str]] = {}
    for tool in app.core.registry:
        marker = "!" if tool.mutating else " "
        by_category.setdefault(tool.category, []).append(f"{marker}{tool.name}")
    lines = [f"{len(app.core.registry)} tools ('!' mutates state):"]
    for category, names in sorted(by_category.items()):
        lines.append(f"  {category}: {' '.join(sorted(names))}")
    lines.append("\n/tools <name> for detail.")
    return CommandResult("\n".join(lines))


def _context(app: LocalAIApp, args: list[str]) -> CommandResult:
    """Show exactly what will be sent to the model. No hidden context anywhere."""
    messages = app.state.messages
    limit = app.state.context_limit
    used = sum(m.approx_tokens() for m in messages)
    lines = [
        f"Context: ~{used:,} of {limit:,} tokens ({used / limit * 100:.0f}%)",
        "(token counts here are estimated at ~4 chars/token; Ollama reports exact "
        "prompt counts after each generation)",
        "",
        f"{len(messages)} message(s) will be sent, in this order:",
    ]
    for index, message in enumerate(messages):
        preview = " ".join(message.content.split())[:80]
        calls = f" +{len(message.tool_calls)} tool call(s)" if message.tool_calls else ""
        lines.append(
            f"  {index:>3}. {message.role.value:<9} ~{message.approx_tokens():>6,} tok{calls}  {preview}"
        )
    if app.state.system_prompt:
        lines += ["", "System prompt is message 0 above."]
    return CommandResult("\n".join(lines))


def _usage(app: LocalAIApp, args: list[str]) -> CommandResult:
    from localai.storage.usage import Period

    period = Period(args[0]) if args and args[0] in {p.value for p in Period} else Period.TODAY
    totals = app.core.usage.totals(period)
    if totals.generations == 0:
        return CommandResult(f"No usage recorded for '{period.value}'.")

    note = {
        "exact": "exact (reported by Ollama)",
        "mixed": "mixed: some generations reported, some not",
        "estimated": "estimated",
        "unknown": "not reported by Ollama",
    }[totals.exactness]
    lines = [
        f"Usage - {period.value}  [{note}]",
        f"  prompt      {totals.format_tokens(totals.prompt_tokens):>14}",
        f"  completion  {totals.format_tokens(totals.completion_tokens):>14}",
        f"  thinking    ~{totals.thinking_tokens:>13,}  (always estimated)",
        f"  total       {totals.format_tokens(totals.total_tokens):>14}",
        f"  generations {totals.generations:>14,}",
        f"  tool calls  {totals.tool_calls:>14,}",
    ]
    if tps := totals.tokens_per_second:
        lines.append(f"  tok/s       {tps:>14,.1f}")
    if totals.models:
        lines += ["", "By model:"]
        lines += [f"  {name:<28} {count:>5} generation(s)" for name, count in totals.models.items()]
    return CommandResult("\n".join(lines))


def _history(app: LocalAIApp, args: list[str]) -> CommandResult:
    if args:
        return CommandResult(action="search_history", data={"query": " ".join(args)})
    return CommandResult(action="open_history")


def _resume(app: LocalAIApp, args: list[str]) -> CommandResult:
    if not args:
        return CommandResult(action="open_history")
    return CommandResult(action="resume", data={"conversation_id": args[0]})


def _new(app: LocalAIApp, args: list[str]) -> CommandResult:
    return CommandResult(action="new_conversation", data={"title": " ".join(args)})


def _fork(app: LocalAIApp, args: list[str]) -> CommandResult:
    if not args or not args[0].isdigit():
        return CommandResult("usage: /fork <message-number>  (see numbers with /context)", "error")
    return CommandResult(action="fork", data={"seq": int(args[0])})


def _export(app: LocalAIApp, args: list[str]) -> CommandResult:
    fmt = args[0] if args and args[0] in {"md", "json"} else "md"
    return CommandResult(action="export", data={"format": fmt})


def _system(app: LocalAIApp, args: list[str]) -> CommandResult:
    if not args:
        current = app.state.system_prompt or "(none)"
        return CommandResult(f"System prompt:\n{current}\n\nSet with: /system <text>")
    app.state.system_prompt = " ".join(args)
    return CommandResult("System prompt updated. It applies from the next message.", "success")


def _think(app: LocalAIApp, args: list[str]) -> CommandResult:
    if not app.state.model_info or not app.state.model_info.supports_thinking:
        return CommandResult(
            f"{app.state.model} does not expose reasoning. Try a thinking-capable model "
            "such as qwen3:8b.",
            "warning",
        )
    app.state.think = True
    return CommandResult("Thinking enabled.", "success")


def _nothink(app: LocalAIApp, args: list[str]) -> CommandResult:
    app.state.think = False
    return CommandResult("Thinking disabled.", "success")


def _clear(app: LocalAIApp, args: list[str]) -> CommandResult:
    return CommandResult(action="clear")


def _compact(app: LocalAIApp, args: list[str]) -> CommandResult:
    return CommandResult(action="compact")


def _profile(app: LocalAIApp, args: list[str]) -> CommandResult:
    profiles = app.core.config.profiles
    if not args:
        if not profiles:
            return CommandResult("No profiles defined. Add them under [profiles] in config.toml.")
        lines = ["Model profiles:"]
        lines += [
            f"  {name:<18} {p.model:<20} {p.description or ''}" for name, p in profiles.items()
        ]
        return CommandResult("\n".join(lines))

    profile = profiles.get(args[0])
    if profile is None:
        return CommandResult(
            f"no profile named {args[0]!r}. Available: {', '.join(profiles) or '(none)'}",
            "error",
        )
    return CommandResult(action="apply_profile", data={"name": args[0]})


def _memory(app: LocalAIApp, args: list[str]) -> CommandResult:
    return CommandResult(
        "Persistent memory is not implemented in this phase. When it ships it will be "
        "opt-in, stored in the 'memories' table, and fully inspectable and deletable "
        "from here. Nothing is being remembered between sessions today beyond the "
        "conversations you can see in /history.",
        "info",
    )


def _index(app: LocalAIApp, args: list[str]) -> CommandResult:
    return CommandResult(
        "Document indexing ships in Phase 3. Today you can search files with the "
        "search_file_contents tool and past conversations with /history <query>.",
        "info",
    )


def _search(app: LocalAIApp, args: list[str]) -> CommandResult:
    if not args:
        return CommandResult("usage: /search <text>", "error")
    return CommandResult(action="search_history", data={"query": " ".join(args)})


def _settings(app: LocalAIApp, args: list[str]) -> CommandResult:
    return CommandResult(action="open_settings")


def _logs(app: LocalAIApp, args: list[str]) -> CommandResult:
    from localai.storage.audit_store import AuditStore

    entries = AuditStore(app.core.database).recent(limit=25)
    if not entries:
        return CommandResult("No audit entries yet.")
    import time as _time

    lines = [f"Audit log (most recent {len(entries)}), full log: {app.core.paths.audit_log}"]
    for entry in entries:
        stamp = _time.strftime("%H:%M:%S", _time.localtime(entry["ts"]))
        lines.append(f"  {stamp}  {entry['effect']:<7} {entry['tool']:<20} {entry['outcome']}")
        if entry["effect"] == "deny":
            lines.append(f"           {entry['reason'][:100]}")
    return CommandResult("\n".join(lines))


def _doctor(app: LocalAIApp, args: list[str]) -> CommandResult:
    return CommandResult(action="doctor")


def _quit(app: LocalAIApp, args: list[str]) -> CommandResult:
    return CommandResult(action="quit")


COMMANDS = CommandRegistry()
for _command in (
    SlashCommand("help", "show commands and keyboard shortcuts", _help, "/help [command]", ("?",)),
    SlashCommand("models", "browse and switch installed models", _models),
    SlashCommand("model", "show or set the current model", _model, "/model [name]"),
    SlashCommand("settings", "edit configuration", _settings),
    SlashCommand("theme", "change the colour theme", _theme, "/theme [name]"),
    SlashCommand(
        "permissions",
        "show policy; manage kill switch and grants",
        _permissions,
        "/permissions [killswitch on|off | clear]",
    ),
    SlashCommand(
        "mode",
        "change permission mode or a safety toggle",
        _mode,
        "/mode manual|auto|workspace|bypass | readonly on|off | dry-run on|off",
    ),
    SlashCommand(
        "workspace",
        "set the working directory or manage trusted workspaces",
        _workspace,
        "/workspace [path | add <path> | remove <path>]",
    ),
    SlashCommand("tools", "list tools, or describe one", _tools, "/tools [name]"),
    SlashCommand("context", "show exactly what will be sent to the model", _context),
    SlashCommand("usage", "token and time usage", _usage, "/usage [today|7d|week|month|all]"),
    SlashCommand("history", "browse or search past conversations", _history, "/history [query]"),
    SlashCommand("resume", "resume a saved conversation", _resume, "/resume [id]"),
    SlashCommand("new", "start a new conversation", _new, "/new [title]"),
    SlashCommand("fork", "branch from an earlier message", _fork, "/fork <message-number>"),
    SlashCommand("export", "export this conversation", _export, "/export [md|json]"),
    SlashCommand("index", "document index (Phase 3)", _index),
    SlashCommand("search", "search conversation history", _search, "/search <text>"),
    SlashCommand("memory", "persistent memory (Phase 3)", _memory),
    SlashCommand("profile", "list or apply a model profile", _profile, "/profile [name]"),
    SlashCommand("system", "show or set the system prompt", _system, "/system [text]"),
    SlashCommand("think", "enable reasoning where supported", _think),
    SlashCommand("nothink", "disable reasoning", _nothink),
    SlashCommand("clear", "clear the conversation view", _clear),
    SlashCommand("compact", "summarise older context to free tokens", _compact),
    SlashCommand("logs", "show the audit log", _logs),
    SlashCommand("doctor", "run environment diagnostics", _doctor),
    SlashCommand("quit", "exit", _quit, aliases=("exit", "q")),
):
    COMMANDS.add(_command)
