"""Environment diagnostics.

``ai doctor`` is the first thing a user runs when something is wrong and the
first thing an AI coding agent should run before making changes. Every check reports
``ok``/``warn``/``fail`` plus a remediation string, so the output is actionable
rather than merely descriptive.

The command exits 0 when nothing failed, 1 when any check failed. Warnings alone do
not fail the command -- a missing optional extractor should not break a CI gate.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from importlib import util as import_util
from typing import Any

from localai.app import Application
from localai.config.manager import ENV_OVERRIDES
from localai.version import APP_VERSION


@dataclass(slots=True)
class Check:
    name: str
    status: str
    """``ok`` | ``warn`` | ``fail``"""

    detail: str
    remediation: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "remediation": self.remediation,
            **({"data": self.data} if self.data else {}),
        }


async def run_checks(app: Application) -> list[Check]:
    """Run every diagnostic. Never raises: a failing check is data, not an exception."""
    checks: list[Check] = [
        _python_check(),
        _sqlite_check(),
        await _ollama_check(app),
        await _models_check(app),
        _database_check(app),
        _config_check(app),
        _permissions_check(app),
        _powershell_check(),
        _git_check(),
        _disk_check(app),
        _extractors_check(),
        _privacy_check(app),
        _env_check(app),
    ]
    return checks


def _python_check() -> Check:
    version = sys.version_info
    if version < (3, 11):
        return Check(
            "python",
            "fail",
            f"Python {version.major}.{version.minor} is too old",
            "Install Python 3.11 or newer.",
        )
    return Check(
        "python",
        "ok",
        f"Python {version.major}.{version.minor}.{version.micro}",
        data={"executable": sys.executable},
    )


def _sqlite_check() -> Check:
    try:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        connection.close()
        fts = True
    except sqlite3.OperationalError:
        fts = False
    return Check(
        "sqlite",
        "ok" if fts else "warn",
        f"SQLite {sqlite3.sqlite_version}" + ("" if fts else " (no FTS5)"),
        "" if fts else "Conversation search will use slower substring matching.",
        {"version": sqlite3.sqlite_version, "fts5": fts},
    )


async def _ollama_check(app: Application) -> Check:
    reachable, detail = await app.provider.health()
    if not reachable:
        return Check(
            "ollama",
            "fail",
            detail,
            "Start Ollama with 'ollama serve', or launch the Ollama desktop app. "
            f"Confirm the address in {app.paths.config_file} under [ollama].",
            {"base_url": app.config.ollama.base_url},
        )
    return Check("ollama", "ok", detail, data={"base_url": app.config.ollama.base_url})


async def _models_check(app: Application) -> Check:
    try:
        models = await app.provider.list_models()
    except Exception as exc:
        return Check(
            "models",
            "fail",
            f"could not list models: {exc}",
            "Check that Ollama is running, then retry.",
        )
    if not models:
        return Check(
            "models",
            "warn",
            "no models installed",
            "Install one with: ollama pull qwen3:8b",
            {"count": 0},
        )

    # Classify via the same code `ai providers scan` uses, so the two commands
    # cannot disagree about which models are actually usable as agents.
    from localai.providers.discovery import classify_model

    classified = [classify_model(m) for m in models]
    with_tools = [d.name for d in classified if d.recommended_for_agent]
    loaded = [m.name for m in models if m.loaded]
    best = max(
        classified,
        key=lambda d: (d.recommended_for_agent, d.info.loaded, d.info.context_length),
        default=None,
    )

    status = "ok" if with_tools else "warn"
    detail = f"{len(models)} model(s); {len(with_tools)} with native tool calling"
    if best is not None:
        detail += f"; best for agent use: {best.name}"
    return Check(
        "models",
        status,
        detail,
        ""
        if with_tools
        else "Install a tool-capable model for full agent support: ollama pull qwen3:8b",
        {
            "count": len(models),
            "names": [m.name for m in models],
            "tool_capable": with_tools,
            "thinking_capable": [m.name for m in models if m.supports_thinking],
            "vision_capable": [m.name for m in models if m.supports_vision],
            "loaded": loaded,
            "recommended_model": best.name if best else None,
            "support_levels": {d.name: d.support.value for d in classified},
        },
    )


def _database_check(app: Application) -> Check:
    health = app.database.health()
    status_info = app.database.migration_status()
    if not health.get("ok"):
        return Check(
            "database",
            "fail",
            f"integrity check failed: {health.get('integrity')}",
            f"Back up and remove {app.paths.database}; it will be recreated on next launch.",
            health,
        )
    if status_info["pending"]:
        return Check(
            "database",
            "warn",
            f"{len(status_info['pending'])} migration(s) pending",
            "Run: ai migrate",
            status_info,
        )
    return Check(
        "database",
        "ok",
        f"schema v{status_info['current_version']}, "
        f"{health['size_bytes'] / 1024:.0f} KB, integrity ok",
        data={**health, **status_info},
    )


def _config_check(app: Application) -> Check:
    if not app.paths.config_file.exists():
        return Check(
            "config",
            "warn",
            "no config file; using defaults",
            "Create one with: ai config init",
            {"path": str(app.paths.config_file)},
        )
    return Check(
        "config",
        "ok",
        f"valid, schema v{app.config.schema_version}",
        data={
            "path": str(app.paths.config_file),
            "env_overrides_applied": app.config_manager.applied_env_vars,
        },
    )


def _permissions_check(app: Application) -> Check:
    perms = app.config.permissions
    safety = app.config.safety
    notes: list[str] = []
    status = "ok"

    if perms.mode.value == "bypass":
        status = "warn"
        notes.append("BYPASS mode is active: tools run without per-action confirmation")
    if perms.kill_switch:
        notes.append("kill switch engaged: mutating and executing tools are disabled")
    if safety.read_only:
        notes.append("read-only mode: no tool may modify anything")
    if safety.follow_symlinks:
        status = "warn"
        notes.append("follow_symlinks is on: junctions can lead outside a workspace")
    if not safety.use_recycle_bin:
        status = "warn"
        notes.append("deletions are permanent (use_recycle_bin is off)")
    if not perms.workspaces:
        notes.append("no trusted workspaces defined")

    return Check(
        "permissions",
        status,
        f"mode={perms.mode.value}" + ("; " + "; ".join(notes) if notes else ""),
        "Review with: ai permissions show" if status != "ok" else "",
        {
            "mode": perms.mode.value,
            "kill_switch": perms.kill_switch,
            "read_only": safety.read_only,
            "dry_run": safety.dry_run,
            "workspaces": [str(w.path) for w in perms.workspaces],
            "rules": len(perms.rules),
        },
    )


def _powershell_check() -> Check:
    from localai.tools.shell import find_powershell

    shell = find_powershell()
    if shell is None:
        return Check(
            "powershell",
            "warn",
            "not found",
            "Install PowerShell 7 (pwsh) to enable the run_powershell tool.",
        )
    return Check("powershell", "ok", str(shell), data={"path": str(shell)})


def _git_check() -> Check:
    git = shutil.which("git")
    if git is None:
        return Check(
            "git",
            "warn",
            "not found",
            "Install Git from https://git-scm.com to enable the git tool and checkpoints.",
        )
    return Check("git", "ok", git, data={"path": git})


def _disk_check(app: Application) -> Check:
    try:
        usage = shutil.disk_usage(app.paths.home)
    except OSError as exc:
        return Check("disk", "warn", f"cannot read disk usage: {exc}")

    free_gb = usage.free / 1024**3
    if free_gb < 1:
        return Check(
            "disk",
            "fail",
            f"{free_gb:.1f} GB free on the localai volume",
            "Free disk space; the database and backups need room to grow.",
        )
    status = "warn" if free_gb < 5 else "ok"
    return Check(
        "disk",
        status,
        f"{free_gb:.1f} GB free",
        "Consider freeing space." if status == "warn" else "",
        {"free_bytes": usage.free, "total_bytes": usage.total},
    )


def _extractors_check() -> Check:
    """Report which optional document extractors are installed."""
    modules = {
        "pypdf": "PDF",
        "docx": "Word (.docx)",
        "openpyxl": "Excel (.xlsx)",
        "pptx": "PowerPoint (.pptx)",
        "striprtf": "RTF",
        "bs4": "HTML",
    }
    installed = {name: import_util.find_spec(name) is not None for name in modules}
    missing = [modules[n] for n, present in installed.items() if not present]
    if not missing:
        return Check("extractors", "ok", "all optional extractors installed", data=installed)
    return Check(
        "extractors",
        "warn",
        f"{len(missing)} extractor(s) unavailable: {', '.join(missing)}",
        'Install with: pip install "localai[extract]"',
        installed,
    )


def _privacy_check(app: Application) -> Check:
    ollama = app.config.ollama
    if not ollama.is_loopback:
        return Check(
            "privacy",
            "warn",
            f"Ollama host is {ollama.host}, which is not loopback: prompts leave this machine",
            "Set ollama.host to 127.0.0.1 for local-only operation.",
            {"host": ollama.host, "loopback": False},
        )
    return Check(
        "privacy",
        "ok",
        "local only: Ollama on loopback, no telemetry, no analytics",
        data={
            "host": ollama.host,
            "loopback": True,
            "network_disabled": app.config.privacy.network_disabled,
            "telemetry": False,
        },
    )


def _env_check(app: Application) -> Check:
    """Report which documented environment variables are currently set."""
    active = {name: os.environ[name] for name in ENV_OVERRIDES if os.environ.get(name)}
    if not active:
        return Check("environment", "ok", "no environment overrides active")
    return Check(
        "environment",
        "warn",
        f"{len(active)} override(s) active: {', '.join(active)}",
        "These take precedence over config.toml. Unset them if unexpected.",
        active,
    )


def summarise(checks: list[Check]) -> dict[str, Any]:
    """Build the JSON document for ``ai doctor --json``."""
    counts = {
        status: sum(1 for c in checks if c.status == status) for status in ("ok", "warn", "fail")
    }
    return {
        "ok": counts["fail"] == 0,
        "version": APP_VERSION,
        "summary": counts,
        "checks": [c.to_dict() for c in checks],
    }


def render(checks: list[Check], *, color: bool = True) -> str:
    """Human-readable diagnostic report."""
    from localai.cli.output import style

    symbols = {"ok": ("+", "green"), "warn": ("!", "yellow"), "fail": ("x", "red")}
    lines = [style(f"localai {APP_VERSION} diagnostics", "bold", enabled=color), ""]
    for check in checks:
        symbol, colour = symbols.get(check.status, ("?", "dim"))
        lines.append(
            f" {style(symbol, colour, enabled=color)} "
            f"{style(check.name.ljust(12), 'bold', enabled=color)} {check.detail}"
        )
        if check.remediation:
            lines.append(f"   {style('-> ' + check.remediation, 'dim', enabled=color)}")

    counts = {s: sum(1 for c in checks if c.status == s) for s in ("ok", "warn", "fail")}
    lines += [
        "",
        f"{counts['ok']} ok, {counts['warn']} warning(s), {counts['fail']} failure(s)",
    ]
    return "\n".join(lines)


def doctor_sync(app: Application) -> list[Check]:
    """Synchronous wrapper, for callers not already in an event loop."""
    return asyncio.run(run_checks(app))
