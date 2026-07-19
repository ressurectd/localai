"""Command-line interface.

Built on ``argparse`` rather than a third-party CLI framework: the command surface
is a plain noun-verb tree, stdlib argparse handles it cleanly, and it keeps the
dependency list short for something every install runs.

Every command follows the same contract -- see :mod:`localai.cli.output`. Commands
are small functions taking ``(args, mode)`` and returning an exit code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from localai import logging_setup
from localai.app import Application
from localai.cli import doctor as doctor_module
from localai.cli.output import (
    OutputMode,
    emit_json,
    emit_ndjson,
    fail,
    humanise_bytes,
    info,
    sparkline,
    style,
    table,
    warn,
)
from localai.config.models import Config, PermissionMode
from localai.config.paths import AppPaths
from localai.domain.messages import Message, Role
from localai.errors import LocalAIError
from localai.permissions.engine import Interface, PermissionRequest
from localai.storage.usage import Period
from localai.version import APP_VERSION, version_info


def build_parser() -> argparse.ArgumentParser:
    """Construct the full command tree."""
    parser = argparse.ArgumentParser(
        prog="localai",
        description="Local-first agentic terminal UI for Ollama models.",
        epilog="Run 'localai' with no arguments to launch the interactive interface.",
    )
    parser.add_argument("--version", action="store_true", help="print version information")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="emit JSON on stdout")
    common.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    common.add_argument("-q", "--quiet", action="store_true", help="suppress non-essential output")
    common.add_argument("-v", "--verbose", action="store_true", help="verbose logging on stderr")
    common.add_argument("--home", type=Path, help="override the localai state directory")
    common.add_argument("--cwd", type=Path, help="working directory for tools")
    common.add_argument("--mock", action="store_true", help="use the deterministic mock provider")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], help=help_text, description=help_text)

    # --- introspection ---
    add("doctor", "check the environment and report problems")
    add("version", "print version information for every interface")
    add("capabilities", "describe what this build can do")
    add("architecture", "print the module architecture")

    models = add("models", "inspect installed models").add_subparsers(dest="subcommand")
    models.add_parser("list", parents=[common], help="list installed models")
    show = models.add_parser("show", parents=[common], help="show one model in detail")
    show.add_argument("name")
    models.add_parser("loaded", parents=[common], help="list models resident in memory")
    preload = models.add_parser("preload", parents=[common], help="load a model into memory")
    preload.add_argument("name")
    preload.add_argument("--keep-alive", default="-1", help="'-1' pins it indefinitely")
    unload = models.add_parser("unload", parents=[common], help="evict a model from memory")
    unload.add_argument("name")

    tools = add("tools", "inspect the tool registry").add_subparsers(dest="subcommand")
    tools.add_parser("list", parents=[common], help="list available tools")
    describe = tools.add_parser("describe", parents=[common], help="describe one tool")
    describe.add_argument("name")
    schema = tools.add_parser("schema", parents=[common], help="print a tool's JSON Schema")
    schema.add_argument("name")

    config_cmd = add("config", "inspect and validate configuration").add_subparsers(
        dest="subcommand"
    )
    config_cmd.add_parser("validate", parents=[common], help="validate the config file")
    config_cmd.add_parser("show", parents=[common], help="print effective configuration")
    config_cmd.add_parser("schema", parents=[common], help="print the config JSON Schema")
    config_cmd.add_parser("path", parents=[common], help="print config and data locations")
    config_cmd.add_parser("init", parents=[common], help="write a default config file")

    perms = add("permissions", "inspect the permissions engine").add_subparsers(dest="subcommand")
    perms.add_parser("show", parents=[common], help="show the active policy")
    perms.add_parser("validate", parents=[common], help="validate permission rules")
    explain = perms.add_parser(
        "explain", parents=[common], help="explain the decision for a proposed action"
    )
    explain.add_argument(
        "--action", type=Path, required=True, help="JSON file describing the action"
    )

    usage_cmd = add("usage", "report token and time usage").add_subparsers(dest="subcommand")
    report = usage_cmd.add_parser("report", parents=[common], help="usage report")
    report.add_argument(
        "--period",
        default="all",
        choices=[p.value for p in Period],
        help="reporting window",
    )

    migrations = add("migrations", "database schema migrations").add_subparsers(dest="subcommand")
    migrations.add_parser("status", parents=[common], help="show migration state")
    migrations.add_parser("apply", parents=[common], help="apply pending migrations")

    history = add("history", "browse saved conversations").add_subparsers(dest="subcommand")
    history_list = history.add_parser("list", parents=[common], help="list conversations")
    history_list.add_argument("--limit", type=int, default=20)
    history_search = history.add_parser("search", parents=[common], help="search message text")
    history_search.add_argument("query")
    history_export = history.add_parser("export", parents=[common], help="export a conversation")
    history_export.add_argument("conversation_id")
    history_export.add_argument("--format", choices=["md", "json"], default="md")
    history_export.add_argument("--output", type=Path)

    logs = add("logs", "read the audit log")
    logs.add_argument("--limit", type=int, default=30)
    logs.add_argument("--tool", help="filter by tool name")
    logs.add_argument("--effect", choices=["allow", "confirm", "deny", "event"])

    # --- action ---
    chat = add("chat", "send one prompt and print the reply")
    chat.add_argument("--model", help="model tag; defaults to config or the first installed")
    chat.add_argument("--prompt", required=True)
    chat.add_argument("--system", help="system prompt")
    chat.add_argument("--profile", help="named model profile to apply")
    chat.add_argument("--temperature", type=float)
    chat.add_argument("--seed", type=int)
    chat.add_argument("--think", action="store_true", help="enable reasoning where supported")
    chat.add_argument("--no-think", action="store_true", help="disable reasoning")
    chat.add_argument("--tools", action="store_true", help="allow tool use")
    chat.add_argument("--stream", action="store_true", help="stream NDJSON events")
    chat.add_argument("--save", action="store_true", help="persist the conversation")
    chat.add_argument(
        "--permission-mode", choices=[m.value for m in PermissionMode], help="override for this run"
    )
    chat.add_argument("--timeout", type=float, help="seconds before the turn is abandoned")

    test_tool = add("test-tool", "invoke one tool directly, for development")
    test_tool.add_argument("name")
    test_tool.add_argument("--args-file", type=Path, help="JSON file of arguments")
    test_tool.add_argument("--args", help="JSON string of arguments")
    test_tool.add_argument("--yes", action="store_true", help="auto-confirm when policy asks")

    run_cmd = add("run", "launch the interactive terminal UI")
    run_cmd.add_argument("--model")
    run_cmd.add_argument("--workspace", type=Path)
    run_cmd.add_argument("--permission-mode", choices=[m.value for m in PermissionMode])
    run_cmd.add_argument("--resume", help="conversation id to resume")

    providers = add("providers", "discover model providers installed on this machine")
    providers_sub = providers.add_subparsers(dest="subcommand")
    scan = providers_sub.add_parser(
        "scan", parents=[common], help="scan for providers and installed models"
    )
    scan.add_argument(
        "--include-mock", action="store_true", help="also list the built-in mock provider"
    )
    providers_sub.add_parser(
        "best", parents=[common], help="print the model best suited to agentic use"
    )

    dev = add("dev", "development helpers").add_subparsers(dest="subcommand")
    dev.add_parser("sandbox", parents=[common], help="run the UI against synthetic data")

    return parser


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


def _build_app(args: argparse.Namespace, interface: Interface = Interface.CLI) -> Application:
    overrides: dict[str, Any] = {}
    if mode := getattr(args, "permission_mode", None):
        overrides["permissions.mode"] = mode
    return Application.create(
        home=getattr(args, "home", None),
        cwd=getattr(args, "cwd", None),
        interface=interface,
        use_mock=bool(getattr(args, "mock", False)),
        overrides=overrides,
    )


async def cmd_doctor(args: argparse.Namespace, mode: OutputMode) -> int:
    async with _build_app(args) as app:
        checks = await doctor_module.run_checks(app)
        payload = doctor_module.summarise(checks)
        if mode.json:
            emit_json(payload, schema="localai.doctor/1")
        else:
            print(doctor_module.render(checks, color=mode.color))
        return 0 if payload["ok"] else 1


def cmd_version(args: argparse.Namespace, mode: OutputMode) -> int:
    if mode.json:
        emit_json({"versions": version_info()}, schema="localai.version/1")
    else:
        print(f"localai {APP_VERSION}")
        for key, value in version_info().items():
            if key != "app":
                print(f"  {key:<20} {value}")
    return 0


async def cmd_capabilities(args: argparse.Namespace, mode: OutputMode) -> int:
    """Describe the build so an external agent need not read the source."""
    async with _build_app(args) as app:
        reachable, detail = await app.provider.health()
        payload: dict[str, Any] = {
            "versions": version_info(),
            "provider": {"name": app.provider.name, "reachable": reachable, "detail": detail},
            "tools": [
                {
                    "name": t.name,
                    "risk": t.risk.value,
                    "mutating": t.mutating,
                    "category": t.category,
                }
                for t in app.registry
            ],
            "permission_modes": [m.value for m in PermissionMode],
            "usage_periods": [p.value for p in Period],
            "features": {
                "streaming_chat": True,
                "native_tool_calling": True,
                "structured_tool_fallback": True,
                "permission_engine": True,
                "audit_log": True,
                "usage_tracking": True,
                "conversation_storage": True,
                "conversation_search": app.database.fts_available,
                "recycle_bin_delete": sys.platform == "win32",
                "backups": True,
                "dry_run": True,
                "document_extraction": False,
                "semantic_index": False,
                "investigation_mode": False,
                "local_http_api": False,
                "mcp_server": False,
                "fine_tuning": False,
            },
            "config_path": str(app.paths.config_file),
            "database_path": str(app.paths.database),
        }
        if mode.json:
            emit_json(payload, schema="localai.capabilities/1")
        else:
            print(f"localai {APP_VERSION}")
            print(f"  provider   {app.provider.name} ({'up' if reachable else 'down'}: {detail})")
            print(f"  tools      {len(payload['tools'])}")
            enabled = [k for k, v in payload["features"].items() if v]
            missing = [k for k, v in payload["features"].items() if not v]
            print(f"  enabled    {', '.join(enabled)}")
            print(f"  not built  {', '.join(missing)}")
        return 0


def _render_providers(providers: list, *, color: bool = True) -> str:
    """Human-readable provider discovery report.

    Lives in the CLI rather than in providers/discovery.py: rendering is presentation,
    and the providers layer must not depend on the interface layer. discovery.summarise()
    supplies the same information as pure data for --json consumers.
    """
    from localai.providers.discovery import ProviderStatus, SupportLevel

    lines: list[str] = []
    symbols = {
        ProviderStatus.READY: ("+", "green"),
        ProviderStatus.RUNNING_NO_MODELS: ("!", "yellow"),
        ProviderStatus.INSTALLED_NOT_RUNNING: ("!", "yellow"),
        ProviderStatus.NOT_INSTALLED: ("x", "red"),
        ProviderStatus.UNKNOWN: ("?", "yellow"),
    }

    for provider in providers:
        symbol, colour = symbols[provider.status]
        header = f"{provider.name}{f' {provider.version}' if provider.version else ''}"
        lines.append(
            f"{style(symbol, colour, enabled=color)} "
            f"{style(header, 'bold', enabled=color)}  {provider.detail}"
        )
        if provider.executable:
            lines.append(f"    binary     {provider.executable}")
        if provider.base_url:
            lines.append(f"    endpoint   {provider.base_url}")
        if provider.model_dir:
            lines.append(f"    models in  {provider.model_dir}")
        if provider.remediation:
            lines.append(f"    {style('-> ' + provider.remediation, 'dim', enabled=color)}")

        if provider.models:
            lines.append("")
            best = provider.best_model()
            badges = {
                SupportLevel.FULL: ("full  ", "green"),
                SupportLevel.FALLBACK: ("text  ", "yellow"),
                SupportLevel.CHAT_ONLY: ("chat  ", "dim"),
                SupportLevel.EMBEDDING: ("embed ", "blue"),
            }
            for model in provider.models:
                marker = "*" if best and model.name == best.name else " "
                label, badge_colour = badges[model.support]
                loaded = " [loaded]" if model.info.loaded else ""
                context = f"{model.info.context_length:,}" if model.info.context_length else "?"
                lines.append(
                    f"  {marker} {style(label, badge_colour, enabled=color)} "
                    f"{model.name:<30} {humanise_bytes(model.info.size_bytes):>9}  "
                    f"ctx {context:>7}{loaded}"
                )
                if model.family_note:
                    lines.append(f"      {style(model.family_note, 'dim', enabled=color)}")
        lines.append("")

    lines.append(
        "  * recommended for agentic use   full = native tool calling   text = fallback protocol"
    )
    return "\n".join(lines)


async def cmd_providers(args: argparse.Namespace, mode: OutputMode) -> int:
    """Discover which providers and models are installed on this machine."""
    from localai.providers import discovery

    app = _build_app(args)
    try:
        sub = getattr(args, "subcommand", None) or "scan"

        if sub == "best":
            best = await discovery.find_best_model(app.config.ollama)
            if best is None:
                if mode.json:
                    emit_json({"model": None}, schema="localai.provider-best/1")
                else:
                    info("No usable model found. Install one with: ollama pull qwen3:8b", mode)
                return 1
            if mode.json:
                emit_json({"model": best.to_dict()}, schema="localai.provider-best/1")
            else:
                print(best.name)
            return 0

        found = await discovery.discover_all(
            app.config.ollama, include_mock=getattr(args, "include_mock", False)
        )
        if mode.json:
            emit_json(discovery.summarise(found), schema="localai.providers/1")
        else:
            print(_render_providers(found, color=mode.color))
        # Exit non-zero when nothing is usable, so a script can branch on it.
        return 0 if any(p.usable for p in found) else 69
    finally:
        await app.aclose()


async def cmd_models(args: argparse.Namespace, mode: OutputMode) -> int:
    async with _build_app(args) as app:
        sub = getattr(args, "subcommand", None) or "list"

        if sub == "list":
            models = await app.provider.list_models()
            if mode.json:
                emit_json({"models": [m.to_dict() for m in models]}, schema="localai.models/1")
                return 0
            if not models:
                info("No models installed. Install one with: ollama pull qwen3:8b", mode)
                return 0
            rows = [
                {
                    "name": m.name,
                    "size": humanise_bytes(m.size_bytes),
                    "params": m.parameter_size or "-",
                    "quant": m.quantization or "-",
                    "ctx": f"{m.context_length:,}" if m.context_length else "-",
                    "caps": ",".join(
                        c for c in ("tools", "thinking", "vision") if c in m.capabilities
                    )
                    or "-",
                    "loaded": "yes" if m.loaded else "",
                }
                for m in models
            ]
            print(
                table(
                    rows,
                    [
                        ("name", "MODEL"),
                        ("size", "SIZE"),
                        ("params", "PARAMS"),
                        ("quant", "QUANT"),
                        ("ctx", "CONTEXT"),
                        ("caps", "CAPABILITIES"),
                        ("loaded", "LOADED"),
                    ],
                    mode=mode,
                )
            )
            return 0

        if sub == "show":
            model = await app.provider.show_model(args.name)
            if mode.json:
                emit_json({"model": model.to_dict()}, schema="localai.model/1")
            else:
                data = model.to_dict()
                print(style(model.name, "bold", enabled=mode.color))
                for key, value in data.items():
                    if key != "name":
                        print(f"  {key:<28} {value}")
                if data["estimated_memory_is_estimate"]:
                    print(
                        "\n  Note: memory is estimated from file size (weights x1.2). "
                        "Load the model for an exact figure."
                    )
            return 0

        if sub == "loaded":
            models = await app.provider.loaded_models()
            if mode.json:
                emit_json({"models": [m.to_dict() for m in models]}, schema="localai.models/1")
            elif not models:
                info("No models are currently loaded.", mode)
            else:
                for m in models:
                    print(f"  {m.name:<32} {humanise_bytes(m.vram_bytes)}  expires {m.expires_at}")
            return 0

        if sub == "preload":
            await app.provider.preload(args.name, keep_alive=args.keep_alive)
            info(f"Loaded {args.name} (keep_alive={args.keep_alive})", mode)
            if mode.json:
                emit_json({"model": args.name, "loaded": True}, schema="localai.model-load/1")
            return 0

        if sub == "unload":
            await app.provider.unload(args.name)
            info(f"Unloaded {args.name}", mode)
            if mode.json:
                emit_json({"model": args.name, "loaded": False}, schema="localai.model-load/1")
            return 0

    return 2


def cmd_tools(args: argparse.Namespace, mode: OutputMode) -> int:
    from localai.tools.builtin import register_builtins

    registry = register_builtins()
    sub = getattr(args, "subcommand", None) or "list"

    if sub == "list":
        if mode.json:
            emit_json({"tools": registry.describe_all()}, schema="localai.tools/1")
            return 0
        rows = [
            {
                "name": t.name,
                "category": t.category,
                "risk": t.risk.value,
                "mutating": "yes" if t.mutating else "",
                "description": t.description.split(".")[0][:60],
            }
            for t in registry
        ]
        print(
            table(
                rows,
                [
                    ("name", "TOOL"),
                    ("category", "CATEGORY"),
                    ("risk", "RISK"),
                    ("mutating", "MUTATES"),
                    ("description", "DESCRIPTION"),
                ],
                mode=mode,
            )
        )
        return 0

    tool = registry.get(args.name)
    if sub == "schema":
        if mode.json:
            emit_json(
                {"tool": tool.name, "parameters": tool.parameters}, schema="localai.tool-schema/1"
            )
        else:
            print(json.dumps(tool.parameters, indent=2))
        return 0

    if mode.json:
        emit_json({"tool": tool.to_dict()}, schema="localai.tool/1")
    else:
        print(style(tool.name, "bold", enabled=mode.color))
        print(f"  category  {tool.category}")
        print(f"  risk      {tool.risk.value}{'  (mutating)' if tool.mutating else ''}")
        print(f"\n{tool.description}\n")
        print("Parameters:")
        required = set(tool.parameters.get("required", []))
        for key, spec in tool.parameters.get("properties", {}).items():
            marker = "*" if key in required else " "
            default = f" (default: {spec['default']})" if "default" in spec else ""
            print(
                f"  {marker} {key:<20} {spec.get('type', 'any'):<8} "
                f"{spec.get('description', '')}{default}"
            )
        if required:
            print("\n  * required")
    return 0


def cmd_config(args: argparse.Namespace, mode: OutputMode) -> int:
    paths = AppPaths.resolve(getattr(args, "home", None))
    sub = getattr(args, "subcommand", None) or "show"

    if sub == "schema":
        schema = Config.model_json_schema()
        if mode.json:
            print(json.dumps(schema, indent=2))
        else:
            print(json.dumps(schema, indent=2))
        return 0

    if sub == "path":
        payload: dict[str, Any] = {
            "home": str(paths.home),
            "config": str(paths.config_file),
            "database": str(paths.database),
            "audit_log": str(paths.audit_log),
            "backups": str(paths.backup_dir),
            "logs": str(paths.log_dir),
            "config_exists": paths.config_file.exists(),
        }
        if mode.json:
            emit_json(payload, schema="localai.config-paths/1")
        else:
            for key, value in payload.items():
                print(f"  {key:<16} {value}")
        return 0

    from localai.config.manager import ConfigManager

    manager = ConfigManager(paths)

    if sub == "init":
        paths.ensure()
        created = manager.init_if_missing()
        info(
            f"{'Created' if created else 'Already exists'}: {paths.config_file}",
            mode,
        )
        if mode.json:
            emit_json(
                {"created": created, "path": str(paths.config_file)}, schema="localai.config-init/1"
            )
        return 0

    config = manager.load()  # raises ConfigError, handled by main()

    if sub == "validate":
        payload = {
            "valid": True,
            "path": str(paths.config_file),
            "exists": paths.config_file.exists(),
            "schema_version": config.schema_version,
            "env_overrides": manager.applied_env_vars,
            "warnings": _config_warnings(config),
        }
        if mode.json:
            emit_json(payload, schema="localai.config-validate/1")
        else:
            print(style("configuration is valid", "green", enabled=mode.color))
            for warning in payload["warnings"]:
                warn(warning, mode)
        return 0

    data = config.model_dump(mode="json")
    if mode.json:
        emit_json({"config": data}, schema="localai.config/1")
    else:
        print(json.dumps(data, indent=2, default=str))
    return 0


def _config_warnings(config: Config) -> list[str]:
    """Non-fatal configuration observations worth surfacing."""
    warnings: list[str] = []
    if config.permissions.mode is PermissionMode.BYPASS:
        warnings.append("permission mode is 'bypass': tools run without confirmation")
    if config.safety.follow_symlinks:
        warnings.append("safety.follow_symlinks is on: junctions can escape a workspace")
    if not config.safety.use_recycle_bin:
        warnings.append("safety.use_recycle_bin is off: deletions are permanent")
    if not config.safety.backup_before_modify:
        warnings.append("safety.backup_before_modify is off: edits are not recoverable")
    if not config.ollama.is_loopback:
        warnings.append(f"ollama.host is {config.ollama.host}: prompts leave this machine")
    return warnings


def cmd_permissions(args: argparse.Namespace, mode: OutputMode) -> int:
    app = _build_app(args)
    sub = getattr(args, "subcommand", None) or "show"

    if sub == "show":
        perms = app.config.permissions
        payload: dict[str, Any] = {
            "mode": perms.mode.value,
            "kill_switch": perms.kill_switch,
            "read_only": app.config.safety.read_only,
            "dry_run": app.config.safety.dry_run,
            "workspaces": [
                {
                    "path": str(w.path),
                    "name": w.name,
                    "allow_write": w.allow_write,
                    "allow_execute": w.allow_execute,
                    "exists": w.path.exists(),
                }
                for w in perms.workspaces
            ],
            "rules": [r.model_dump(mode="json") for r in perms.rules],
        }
        if mode.json:
            emit_json(payload, schema="localai.permissions/1")
        else:
            print(f"mode           {payload['mode']}")
            print(f"kill switch    {'ENGAGED' if payload['kill_switch'] else 'off'}")
            print(f"read-only      {payload['read_only']}")
            print(f"dry-run        {payload['dry_run']}")
            print(f"\nworkspaces ({len(payload['workspaces'])}):")
            for workspace in payload["workspaces"]:
                flags = []
                if workspace["allow_write"]:
                    flags.append("write")
                if workspace["allow_execute"]:
                    flags.append("execute")
                missing = "" if workspace["exists"] else "  [MISSING]"
                print(f"  {workspace['path']}  ({', '.join(flags) or 'read only'}){missing}")
            print(f"\nrules ({len(payload['rules'])}):")
            for rule in payload["rules"]:
                print(f"  [{rule['effect']:<7}] {rule['id']}: {rule['note'] or '(no note)'}")
        return 0

    if sub == "validate":
        rules = app.config.permissions.rules
        issues: list[str] = []
        for rule in rules:
            if rule.effect == "allow" and rule.allow_sensitive:
                issues.append(
                    f"rule {rule.id!r} allows access to sensitive paths without confirmation"
                )
            if rule.effect == "allow" and not rule.max_risk:
                issues.append(f"rule {rule.id!r} has no max_risk, so it covers every risk level")
            if rule.expires_at and rule.expires_at < time.time():
                issues.append(f"rule {rule.id!r} expired on {time.ctime(rule.expires_at)}")
        for workspace in app.config.permissions.workspaces:
            if not workspace.path.exists():
                issues.append(f"workspace {workspace.path} does not exist")
        payload = {"valid": True, "rule_count": len(rules), "issues": issues}
        if mode.json:
            emit_json(payload, schema="localai.permissions-validate/1")
        else:
            print(style(f"{len(rules)} rule(s) validated", "green", enabled=mode.color))
            for issue in issues:
                warn(issue, mode)
        return 0

    if sub == "explain":
        try:
            request_data = json.loads(args.action.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"cannot read {args.action}: {exc}", mode)

        request = PermissionRequest.from_dict(request_data)
        decision = app.engine.evaluate(request)
        payload = {"request": request.to_dict(), "decision": decision.to_dict()}
        if mode.json:
            emit_json(payload, schema="localai.permission-explain/1")
        else:
            colour = {"allow": "green", "confirm": "yellow", "deny": "red"}[decision.effect.value]
            print(f"requested action    {request.tool}")
            if request.command:
                print(f"command             {request.command}")
            if request.paths:
                print(f"paths               {', '.join(str(p) for p in request.paths)}")
            print(f"risk classification {decision.risk.value}")
            print(
                f"decision            {style(decision.effect.value.upper(), colour, 'bold', enabled=mode.color)}"
            )
            print(f"evaluation stage    {decision.stage}")
            print(f"matched rule        {decision.matched_rule_id or '(none)'}")
            print(f"needs confirmation  {decision.requires_confirmation}")
            print(f"overridable         {decision.overridable}")
            print(f"workspace boundary  {decision.workspace or '(outside every workspace)'}")
            print(f"reason              {decision.reason}")
            for match in decision.sensitive_matches:
                print(f"  sensitive: {match.kind.value} - {match.reason}")
            for warning in decision.warnings:
                print(f"  warning: {warning}")
        return 0 if decision.effect.value != "deny" else 77

    return 2


def cmd_usage(args: argparse.Namespace, mode: OutputMode) -> int:
    app = _build_app(args)
    period = Period(getattr(args, "period", "all"))
    watts = app.config.usage.assumed_watts if app.config.usage.estimate_energy else None
    payload = app.usage.report(period, assumed_watts=watts)

    if mode.json:
        emit_json(payload, schema="localai.usage-report/1")
        return 0

    totals = payload["totals"]
    if totals["generations"] == 0:
        info(f"No usage recorded for period '{period.value}'.", mode)
        return 0

    marker = {
        "exact": "",
        "mixed": " (mixed exact/estimated)",
        "estimated": " (estimated)",
        "unknown": " (not reported by Ollama)",
    }[totals["token_exactness"]]
    print(style(f"Usage - {period.value}{marker}", "bold", enabled=mode.color))
    print(f"  prompt tokens      {totals['prompt_tokens']:>12,}")
    print(f"  completion tokens  {totals['completion_tokens']:>12,}")
    print(f"  thinking tokens    {totals['thinking_tokens']:>12,}  (always an estimate)")
    print(f"  total tokens       {totals['total_tokens']:>12,}")
    print(f"  generations        {totals['generations']:>12,}")
    print(f"  tool calls         {totals['tool_calls']:>12,}")
    print(f"  generation time    {totals['total_duration_s']:>12,.1f} s")
    if tps := totals["tokens_per_second"]:
        print(f"  tokens/second      {tps:>12,.1f}")
    if totals["energy_wh_estimate"]:
        print(f"  energy (ESTIMATE)  {totals['energy_wh_estimate']:>12,.2f} Wh")

    if payload["by_model"]:
        print("\nBy model:")
        for row in payload["by_model"]:
            exact = "" if row["exact"] else " ~"
            print(
                f"  {row['key']:<32} {row['total_tokens']:>10,}{exact}  {row['generations']:>4} gen"
            )

    daily = payload["daily"]
    if any(d["total_tokens"] for d in daily):
        print(f"\nLast {len(daily)} days: {sparkline([d['total_tokens'] for d in daily])}")
        print(f"  {daily[0]['day']} -> {daily[-1]['day']}")
    return 0


def cmd_migrations(args: argparse.Namespace, mode: OutputMode) -> int:
    app = _build_app(args)
    sub = getattr(args, "subcommand", None) or "status"
    if sub == "apply":
        applied = app.database.migrate()
        info(f"Applied {len(applied)} migration(s): {applied or 'none pending'}", mode)
    payload = app.database.migration_status()
    if mode.json:
        emit_json(payload, schema="localai.migrations/1")
    else:
        print(f"database   {payload['database']}")
        print(f"version    {payload['current_version']} of {payload['latest_version']}")
        print(f"fts5       {payload['fts5_available']}")
        for migration in payload["migrations"]:
            state = "applied" if migration["applied"] else "PENDING"
            print(f"  {migration['version']:03d} {migration['name']:<24} {state}")
        for warning in payload["warnings"]:
            warn(warning, mode)
    return 0


def cmd_history(args: argparse.Namespace, mode: OutputMode) -> int:
    app = _build_app(args)
    sub = getattr(args, "subcommand", None) or "list"

    if sub == "list":
        records = app.conversations.recent(limit=args.limit)
        if mode.json:
            emit_json({"conversations": [r.to_dict() for r in records]}, schema="localai.history/1")
            return 0
        if not records:
            info("No saved conversations.", mode)
            return 0
        rows = [
            {
                "id": r.id,
                "title": (r.title or "(untitled)")[:40],
                "model": r.model or "-",
                "messages": r.message_count,
                "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(r.updated_at)),
            }
            for r in records
        ]
        print(
            table(
                rows,
                [
                    ("id", "ID"),
                    ("title", "TITLE"),
                    ("model", "MODEL"),
                    ("messages", "MSGS"),
                    ("updated", "UPDATED"),
                ],
                mode=mode,
            )
        )
        return 0

    if sub == "search":
        results = app.conversations.search(args.query)
        if mode.json:
            emit_json({"query": args.query, "results": results}, schema="localai.history-search/1")
            return 0
        if not results:
            info(f"No messages matching {args.query!r}.", mode)
            return 0
        for hit in results:
            stamp = time.strftime("%Y-%m-%d", time.localtime(hit["created_at"]))
            print(
                f"{style(hit['conversation_id'], 'cyan', enabled=mode.color)}  "
                f"{stamp}  {hit['role']}"
            )
            print(f"  {hit['snippet']}\n")
        return 0

    if sub == "export":
        destination = args.output or Path(f"{args.conversation_id}.{args.format}")
        try:
            written = app.conversations.export_to_file(
                args.conversation_id, destination, fmt=args.format
            )
        except KeyError:
            fail(f"no conversation with id {args.conversation_id!r}", mode)
        info(f"Exported to {written}", mode)
        if mode.json:
            emit_json({"path": str(written)}, schema="localai.history-export/1")
        return 0

    return 2


def cmd_logs(args: argparse.Namespace, mode: OutputMode) -> int:
    from localai.storage.audit_store import AuditStore

    app = _build_app(args)
    entries = AuditStore(app.database).recent(limit=args.limit, tool=args.tool, effect=args.effect)
    if mode.json:
        emit_json({"entries": entries}, schema="localai.audit/1")
        return 0
    if not entries:
        info("No audit entries.", mode)
        return 0
    colours = {"allow": "green", "confirm": "yellow", "deny": "red", "event": "blue"}
    for entry in entries:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["ts"]))
        effect = style(
            entry["effect"].upper()[:7].ljust(7),
            colours.get(entry["effect"], "dim"),
            enabled=mode.color,
        )
        print(f"{stamp}  {effect}  {entry['interface']:<4}  {entry['tool']:<20} {entry['outcome']}")
        if entry.get("command"):
            print(f"    $ {entry['command'][:120]}")
        if entry["effect"] == "deny":
            print(f"    {style(entry['reason'][:150], 'dim', enabled=mode.color)}")
    return 0


async def cmd_chat(args: argparse.Namespace, mode: OutputMode) -> int:
    """Single-shot chat, optionally with tools and NDJSON streaming."""
    from localai.agent.loop import AgentLoop, EventKind
    from localai.tools.base import Tool

    async def auto_confirm(tool: Tool, arguments: dict[str, Any], decision: Any) -> bool:
        """Non-interactive: only proceed if the user passed an explicit permission mode.

        A scripted invocation must not silently approve actions the policy wanted
        confirmed. Callers who want unattended tool use say so with
        ``--permission-mode bypass``.
        """
        warn(
            f"policy requires confirmation for {tool.name}: {decision.reason} "
            "(declined; pass --permission-mode to change this)",
            mode,
        )
        return False

    app = _build_app(args)
    app.runner.confirm = auto_confirm

    try:
        model = args.model or app.config.default_model
        if not model:
            models = await app.provider.list_models()
            if not models:
                fail(
                    LocalAIError(
                        "no models installed",
                        remediation="Install one with: ollama pull qwen3:8b",
                    ),
                    mode,
                )
            model = models[0].name

        try:
            model_info = await app.provider.show_model(model)
        except LocalAIError as exc:
            fail(exc, mode)

        profile = app.resolve_profile(args.profile)
        options = profile.options.to_ollama() if profile else {}
        if args.temperature is not None:
            options["temperature"] = args.temperature
        if args.seed is not None:
            options["seed"] = args.seed

        think: bool | str | None = None
        if args.think:
            think = True
        elif args.no_think:
            think = False
        elif profile:
            think = profile.think
        if think and not model_info.supports_thinking:
            warn(f"{model} does not support thinking; ignoring --think", mode)
            think = None

        system_prompt = (
            args.system
            or (profile.system_prompt if profile else None)
            or (app.config.system_prompt)
        )
        messages: list[Message] = []
        if system_prompt:
            messages.append(Message(role=Role.SYSTEM, content=system_prompt))
        messages.append(Message(role=Role.USER, content=args.prompt))

        conversation_id = None
        if args.save:
            record = app.conversations.create(
                title=args.prompt[:60],
                model=model,
                system_prompt=system_prompt,
                workspace=str(app.cwd),
            )
            conversation_id = record.id
            for message in messages:
                app.conversations.add_message(conversation_id, message)

        tools = list(app.registry) if args.tools else []
        cancel = asyncio.Event()
        context = app.tool_context(conversation_id=conversation_id, cancel=cancel)
        loop = AgentLoop(app.provider, app.runner, caller=app.caller)

        content_parts: list[str] = []
        turn = None

        async def drive() -> None:
            nonlocal turn
            async for event in loop.run_turn(
                model=model,
                messages=messages,
                tools=tools,
                context=context,
                model_info=model_info,
                options=options or None,
                think=think,
                keep_alive=profile.keep_alive if profile else None,
            ):
                if args.stream and mode.json:
                    emit_ndjson(_event_to_dict(event))
                elif event.kind is EventKind.CONTENT_DELTA and not mode.json:
                    print(event.text, end="", flush=True)
                elif event.kind is EventKind.TOOL_REQUESTED and not mode.json:
                    print(style(f"\n[tool] {event.text}", "cyan", enabled=mode.color), flush=True)
                elif event.kind is EventKind.TOOL_RESULT and not mode.json and event.tool_result:
                    status = "ok" if event.tool_result.ok else "failed"
                    print(style(f"[tool {status}]", "dim", enabled=mode.color), flush=True)
                elif event.kind in {EventKind.ERROR, EventKind.GUARD_TRIPPED} and not mode.json:
                    print(style(f"\n{event.text}", "red", enabled=mode.color), file=sys.stderr)

                if event.kind is EventKind.CONTENT_DELTA:
                    content_parts.append(event.text)
            turn = loop.last_result

        try:
            await asyncio.wait_for(drive(), timeout=args.timeout) if args.timeout else await drive()
        except TimeoutError:
            cancel.set()
            fail(f"chat timed out after {args.timeout}s", mode, exit_code=124)

        assert turn is not None
        usage = turn.combined_usage()

        if conversation_id:
            for message in turn.messages:
                app.conversations.add_message(conversation_id, message)
            app.usage.record(
                usage,
                model=model,
                conversation_id=conversation_id,
                workspace=str(app.cwd),
                tool_calls=turn.tool_calls,
            )

        if mode.json and not args.stream:
            emit_json(
                {
                    "model": model,
                    "response": "".join(content_parts),
                    "conversation_id": conversation_id,
                    "tool_calls": turn.tool_calls,
                    "iterations": turn.iterations,
                    "cancelled": turn.cancelled,
                    "error": turn.error,
                    "usage": {
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "thinking_tokens": usage.thinking_tokens,
                        "total_tokens": usage.total_tokens,
                        "token_source": usage.token_source.value,
                        "thinking_token_source": usage.thinking_token_source.value,
                        "tokens_per_second": usage.tokens_per_second,
                        "duration_s": round(usage.wall_seconds, 3),
                    },
                },
                schema="localai.chat/1",
            )
        elif not mode.json:
            print()
            if not mode.quiet:
                tps = f", {usage.tokens_per_second:.1f} tok/s" if usage.tokens_per_second else ""
                marker = "" if usage.token_source.value == "reported" else " (estimated)"
                print(
                    style(
                        f"[{usage.prompt_tokens}+{usage.completion_tokens} tokens{marker}{tps}]",
                        "dim",
                        enabled=mode.color,
                    ),
                    file=sys.stderr,
                )

        return 1 if turn.error else 0
    finally:
        await app.aclose()


def _event_to_dict(event: Any) -> dict[str, Any]:
    """Serialise an AgentEvent for NDJSON streaming."""
    payload: dict[str, Any] = {"event": event.kind.value, "iteration": event.iteration}
    if event.text:
        payload["text"] = event.text
    if event.tool_call:
        payload["tool_call"] = event.tool_call.to_dict()
    if event.tool_result:
        payload["tool_result"] = event.tool_result.to_dict()
    if event.usage:
        payload["usage"] = {
            "prompt_tokens": event.usage.prompt_tokens,
            "completion_tokens": event.usage.completion_tokens,
            "token_source": event.usage.token_source.value,
        }
    if event.detail:
        payload["detail"] = event.detail
    return payload


async def cmd_test_tool(args: argparse.Namespace, mode: OutputMode) -> int:
    """Invoke one tool directly. The development and debugging entry point."""
    from localai.tools.base import Tool

    if args.args_file:
        try:
            arguments = json.loads(args.args_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"cannot read {args.args_file}: {exc}", mode)
    elif args.args:
        try:
            arguments = json.loads(args.args)
        except json.JSONDecodeError as exc:
            fail(f"--args is not valid JSON: {exc}", mode)
    else:
        arguments = {}

    async def confirm(tool: Tool, tool_args: dict[str, Any], decision: Any) -> bool:
        if args.yes:
            return True
        warn(f"confirmation required: {decision.reason} (pass --yes to approve)", mode)
        return False

    async with _build_app(args) as app:
        app.runner.confirm = confirm
        decision, preview = await app.runner.preview(args.name, arguments, app.tool_context())
        if not mode.json:
            print(style(f"action:   {preview}", "bold", enabled=mode.color))
            print(f"decision: {decision.effect.value} ({decision.stage}) - {decision.reason}\n")

        started = time.perf_counter()
        result = await app.runner.execute(
            args.name, arguments, app.tool_context(), caller=app.caller
        )
        elapsed = (time.perf_counter() - started) * 1000

        if mode.json:
            emit_json(
                {
                    "tool": args.name,
                    "arguments": arguments,
                    "decision": decision.to_dict(),
                    "result": result.to_dict(),
                    "duration_ms": round(elapsed, 2),
                },
                schema="localai.tool-run/1",
            )
        else:
            print(result.content)
            if result.error:
                print(style(f"\nerror: {result.error}", "red", enabled=mode.color), file=sys.stderr)
            print(
                style(
                    f"\n[{elapsed:.0f} ms, flags: {result.flags or 'none'}]",
                    "dim",
                    enabled=mode.color,
                ),
                file=sys.stderr,
            )
        return 0 if result.ok else 1


def cmd_architecture(args: argparse.Namespace, mode: OutputMode) -> int:
    """Describe the module layout, so an agent can orient without reading everything."""
    layers: dict[str, dict[str, Any]] = {
        "ui": {
            "modules": ["localai.ui.app", "localai.ui.widgets", "localai.ui.commands"],
            "responsibility": "Textual terminal interface. Contains no business logic.",
            "depends_on": ["app", "agent", "storage"],
        },
        "cli": {
            "modules": ["localai.cli.main", "localai.cli.output", "localai.cli.doctor"],
            "responsibility": "Non-interactive interface for scripts and coding agents.",
            "depends_on": ["app", "agent", "storage"],
        },
        "agent": {
            "modules": ["localai.agent.loop", "localai.agent.fallback"],
            "responsibility": "Tool-call loop, guard rails, structured fallback.",
            "depends_on": ["providers", "tools", "domain"],
        },
        "tools": {
            "modules": [
                "localai.tools.base",
                "localai.tools.registry",
                "localai.tools.runner",
                "localai.tools.fs_read",
                "localai.tools.fs_write",
                "localai.tools.shell",
                "localai.tools.system",
            ],
            "responsibility": "Capabilities the model can invoke. runner.py is the "
            "single execution path and enforces policy.",
            "depends_on": ["permissions", "safety", "config"],
            "security_sensitive": ["runner", "shell", "fs_write"],
        },
        "permissions": {
            "modules": ["localai.permissions.engine", "localai.permissions.audit"],
            "responsibility": "One decision point for every action; append-only audit log.",
            "depends_on": ["config", "safety"],
            "security_sensitive": ["engine", "audit"],
        },
        "safety": {
            "modules": [
                "localai.safety.pathsafe",
                "localai.safety.sensitive",
                "localai.safety.injection",
                "localai.safety.backup",
                "localai.safety.recycle",
            ],
            "responsibility": "Path containment, sensitive-data classification, injection "
            "detection, backups, Recycle Bin deletion.",
            "depends_on": [],
            "security_sensitive": ["pathsafe", "sensitive", "injection"],
        },
        "providers": {
            "modules": [
                "localai.providers.base",
                "localai.providers.ollama",
                "localai.providers.mock",
            ],
            "responsibility": "Model backends behind one Protocol.",
            "depends_on": ["domain", "config"],
        },
        "storage": {
            "modules": [
                "localai.storage.db",
                "localai.storage.conversations",
                "localai.storage.usage",
                "localai.storage.audit_store",
            ],
            "responsibility": "SQLite access and migrations. No other module opens the database.",
            "depends_on": ["domain"],
        },
        "config": {
            "modules": ["localai.config.models", "localai.config.manager", "localai.config.paths"],
            "responsibility": "Typed configuration, atomic persistence, path resolution.",
            "depends_on": [],
        },
        "domain": {
            "modules": ["localai.domain.messages"],
            "responsibility": "Provider-neutral conversation primitives.",
            "depends_on": [],
        },
    }
    payload: dict[str, Any] = {
        "composition_root": "localai.app.Application.create",
        "layers": layers,
        "invariants": [
            "Every tool call passes through localai.tools.runner.ToolRunner.execute.",
            "Every permission decision comes from localai.permissions.engine."
            "PermissionEngine.evaluate.",
            "Stages 1-3 of the evaluation order cannot be overridden by configuration.",
            "No module opens sqlite3 directly; all access goes through storage.db.Database.",
            "Logs go to stderr; JSON output goes to stdout.",
            "Tools never receive the permissions engine in their context.",
        ],
    }
    if mode.json:
        emit_json(payload, schema="localai.architecture/1")
    else:
        for name, layer in layers.items():
            marker = " [SECURITY-SENSITIVE]" if layer.get("security_sensitive") else ""
            print(style(f"{name}{marker}", "bold", enabled=mode.color))
            print(f"  {layer['responsibility']}")
            print(f"  depends on: {', '.join(layer['depends_on']) or '(nothing)'}")
            print()
        print(style("Invariants:", "bold", enabled=mode.color))
        for invariant in payload["invariants"]:
            print(f"  - {invariant}")
    return 0


def cmd_run(args: argparse.Namespace, mode: OutputMode, *, sandbox: bool = False) -> int:
    """Launch the Textual interface."""
    try:
        from localai.ui.app import LocalAIApp
    except ImportError as exc:
        fail(
            LocalAIError(
                f"the terminal UI could not be loaded: {exc}",
                remediation="Install the UI dependency with: pip install textual",
            ),
            mode,
        )

    overrides: dict[str, Any] = {}
    if getattr(args, "permission_mode", None):
        overrides["permissions.mode"] = args.permission_mode

    app = Application.create(
        home=getattr(args, "home", None),
        cwd=getattr(args, "workspace", None) or getattr(args, "cwd", None),
        interface=Interface.TUI,
        use_mock=sandbox or bool(getattr(args, "mock", False)),
        overrides=overrides,
    )
    if sandbox:
        # The sandbox must not be able to modify anything, regardless of the user's
        # saved configuration.
        app.config.safety.dry_run = True
        app.config.permissions.mode = PermissionMode.MANUAL

    LocalAIApp(
        app,
        initial_model=getattr(args, "model", None),
        resume_id=getattr(args, "resume", None),
        sandbox=sandbox,
    ).run()
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    mode = OutputMode.from_args(args)

    logging_setup.configure(
        level="DEBUG" if mode.verbose else "WARNING",
        log_file=AppPaths.resolve(getattr(args, "home", None)).app_log,
        quiet=mode.quiet and not mode.verbose,
    )

    if getattr(args, "version", False):
        return cmd_version(args, mode)

    command = getattr(args, "command", None)
    if command is None:
        # Bare `localai` launches the interactive UI, matching user expectation.
        return cmd_run(argparse.Namespace(**vars(args)), mode)

    handlers: dict[str, Any] = {
        "doctor": cmd_doctor,
        "version": cmd_version,
        "capabilities": cmd_capabilities,
        "architecture": cmd_architecture,
        "models": cmd_models,
        "providers": cmd_providers,
        "tools": cmd_tools,
        "config": cmd_config,
        "permissions": cmd_permissions,
        "usage": cmd_usage,
        "migrations": cmd_migrations,
        "history": cmd_history,
        "logs": cmd_logs,
        "chat": cmd_chat,
        "test-tool": cmd_test_tool,
        "run": cmd_run,
        "dev": lambda a, m: cmd_run(a, m, sandbox=True),
    }
    handler = handlers.get(command)
    if handler is None:
        parser.print_help()
        return 2

    try:
        result = handler(args, mode)
        return asyncio.run(result) if asyncio.iscoroutine(result) else int(result)
    except LocalAIError as exc:
        fail(exc, mode)
    except KeyboardInterrupt:
        # Partial work is already persisted: conversations write incrementally and
        # the audit log is append-only, so an interrupt loses nothing committed.
        print(style("\ninterrupted", "yellow", enabled=mode.color), file=sys.stderr)
        return 130
    except BrokenPipeError:
        # `ai models list | head` closes the pipe early; that is not an error.
        return 0


if __name__ == "__main__":
    sys.exit(main())
