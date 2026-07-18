"""System inspection and Git tools.

These are read-only and use standard library facilities or short, fixed command
lines. Nothing here interpolates model-supplied text into a shell string: the Git
tool takes an enum of subcommands rather than an arbitrary argument list, so a
model cannot reach ``git push --force`` through a tool advertised as read-only.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from localai.config.models import RiskLevel
from localai.tools.base import Tool, ToolContext, ToolResult
from localai.tools.runner import path_from


def _humanise(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class SystemInfo(Tool):
    name = "system_info"
    description = (
        "Report information about this computer: operating system, CPU, memory, Python "
        "version and hostname. Use this to understand the environment before suggesting "
        "commands."
    )
    category = "system"
    risk = RiskLevel.READ
    parameters = {"type": "object", "properties": {}}

    def describe_call(self, arguments: dict[str, Any]) -> str:
        return "read system information"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        details: dict[str, Any] = {
            "system": f"{platform.system()} {platform.release()}",
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor() or "unknown",
            "cpu_count": os.cpu_count(),
            "hostname": platform.node(),
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "cwd": str(context.cwd),
            "user": os.environ.get("USERNAME") or os.environ.get("USER", "unknown"),
        }

        # Physical memory has no stdlib accessor on Windows; GlobalMemoryStatusEx via
        # ctypes avoids adding psutil purely for this one figure.
        if sys.platform == "win32":
            try:
                import ctypes

                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                status = MEMORYSTATUSEX()
                status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
                details["memory_total"] = _humanise(status.ullTotalPhys)
                details["memory_available"] = _humanise(status.ullAvailPhys)
                details["memory_used_percent"] = status.dwMemoryLoad
                details["memory_total_bytes"] = status.ullTotalPhys
                details["memory_available_bytes"] = status.ullAvailPhys
            except (OSError, AttributeError):
                details["memory_total"] = "unavailable"

        body = "\n".join(f"  {k:<20} {v}" for k, v in details.items())
        return ToolResult(content=f"System information:\n{body}", metadata=details)


class DiskInfo(Tool):
    name = "disk_info"
    description = (
        "List drives with their total, used and free space. Unavailable or disconnected "
        "drives are reported rather than causing an error."
    )
    category = "system"
    risk = RiskLevel.READ
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Report on one path's volume instead of all drives.",
            }
        },
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "path")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        return f"read disk usage for {arguments.get('path', 'all drives')}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if target := arguments.get("path"):
            candidates = [Path(str(target))]
        elif sys.platform == "win32":
            candidates = [Path(f"{chr(letter)}:/") for letter in range(ord("A"), ord("Z") + 1)]
        else:
            candidates = [Path("/")]

        rows: list[dict[str, Any]] = []
        unavailable: list[str] = []
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                # An unresponsive network or removable drive can block; a short
                # thread-bound timeout keeps one bad drive from freezing the tool.
                usage = await asyncio.wait_for(
                    asyncio.to_thread(shutil.disk_usage, str(candidate)), timeout=5.0
                )
            except (OSError, TimeoutError):
                unavailable.append(str(candidate))
                continue
            rows.append(
                {
                    "drive": str(candidate),
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "percent_used": round(usage.used / usage.total * 100, 1) if usage.total else 0,
                }
            )

        if not rows and not unavailable:
            return ToolResult.failure("no accessible drives found")

        lines = [f"{'Drive':<8} {'Total':>10} {'Used':>10} {'Free':>10}  Used%"]
        for row in rows:
            bar_width = 20
            filled = int(row["percent_used"] / 100 * bar_width)
            bar = "#" * filled + "." * (bar_width - filled)
            lines.append(
                f"{row['drive']:<8} {_humanise(row['total']):>10} {_humanise(row['used']):>10} "
                f"{_humanise(row['free']):>10}  {row['percent_used']:>5.1f}% [{bar}]"
            )
        if unavailable:
            lines.append(f"\nUnavailable (disconnected or not ready): {', '.join(unavailable)}")

        return ToolResult(
            content="\n".join(lines), metadata={"drives": rows, "unavailable": unavailable}
        )


class ListProcesses(Tool):
    name = "list_processes"
    description = (
        "List running processes with their process ID, name and memory usage. Optionally "
        "filter by name."
    )
    category = "system"
    risk = RiskLevel.READ
    returns_untrusted_content = True
    parameters = {
        "type": "object",
        "properties": {
            "filter": {"type": "string", "description": "Case-insensitive name substring."},
            "limit": {"type": "integer", "default": 40, "minimum": 1, "maximum": 500},
            "sort_by": {"type": "string", "enum": ["memory", "name", "pid"], "default": "memory"},
        },
    }

    def describe_call(self, arguments: dict[str, Any]) -> str:
        return f"list processes matching {arguments.get('filter', '(all)')!r}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if sys.platform != "win32":
            return ToolResult.failure("list_processes currently supports Windows only")

        # tasklist with CSV output is parsed structurally rather than by scraping a
        # fixed-width table, which breaks on long process names and localised headers.
        from localai.tools.shell import _stream_process

        code, output, _, timed_out = await _stream_process(
            ["tasklist", "/FO", "CSV", "/NH"],
            cwd=context.cwd,
            timeout=20.0,
            max_bytes=2_000_000,
            cancel=context.cancel,
        )
        if timed_out or code != 0:
            return ToolResult.failure(f"tasklist failed: {output[:300]}")

        import csv
        import io

        needle = str(arguments.get("filter", "")).lower()
        processes: list[dict[str, Any]] = []
        for row in csv.reader(io.StringIO(output)):
            if len(row) < 5:
                continue
            name, pid_text, _, _, memory_text = row[:5]
            if needle and needle not in name.lower():
                continue
            try:
                pid = int(pid_text)
                memory_kb = int(memory_text.replace(",", "").replace(".", "").split()[0])
            except (ValueError, IndexError):
                continue
            processes.append({"name": name, "pid": pid, "memory_kb": memory_kb})

        key = {
            "memory": lambda p: -p["memory_kb"],
            "name": lambda p: p["name"].lower(),
            "pid": lambda p: p["pid"],
        }[arguments.get("sort_by", "memory")]
        processes.sort(key=key)
        limited = processes[: arguments.get("limit", 40)]

        lines = [
            f"{len(processes)} process(es)" + (f" matching {needle!r}" if needle else "") + ":"
        ]
        lines.append(f"{'PID':>8}  {'Memory':>10}  Name")
        lines += [
            f"{p['pid']:>8}  {_humanise(p['memory_kb'] * 1024):>10}  {p['name']}" for p in limited
        ]
        return ToolResult(
            content="\n".join(lines),
            untrusted=True,
            metadata={"total": len(processes), "returned": len(limited), "processes": limited},
        )


class GitCommand(Tool):
    name = "git"
    description = (
        "Run a read-only Git command in a repository: status, log, diff, branch, show, "
        "remote or blame. Use this to understand a repository's state and history."
    )
    category = "system"
    risk = RiskLevel.READ
    returns_untrusted_content = True
    parameters = {
        "type": "object",
        "properties": {
            "subcommand": {
                "type": "string",
                "enum": [
                    "status",
                    "log",
                    "diff",
                    "branch",
                    "show",
                    "remote",
                    "blame",
                    "stash-list",
                ],
                "description": "Which read-only Git command to run.",
            },
            "repository": {"type": "string", "description": "Repository path. Defaults to cwd."},
            "target": {
                "type": "string",
                "description": "Optional file path, commit or ref for diff/show/blame.",
                "maxLength": 200,
            },
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
        },
        "required": ["subcommand"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "repository")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        target = arguments.get("target")
        return f"git {arguments.get('subcommand')}" + (f" {target}" if target else "")

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        git = shutil.which("git")
        if git is None:
            return ToolResult.failure(
                "Git is not installed or not on PATH. Install it from https://git-scm.com."
            )

        repository = Path(str(arguments.get("repository") or context.cwd))
        if not repository.is_dir():
            return ToolResult.failure(f"{repository} is not a directory")

        subcommand = str(arguments["subcommand"])
        limit = int(arguments.get("limit", 20))
        target = arguments.get("target")

        # The argv for each subcommand is fixed here. A model-supplied `target` is
        # appended as a single argv element after `--`, so it cannot introduce a new
        # flag or a second command.
        commands: dict[str, list[str]] = {
            "status": ["status", "--short", "--branch"],
            "log": ["log", f"-{limit}", "--pretty=format:%h %ad %an: %s", "--date=short"],
            "diff": ["diff", "--stat"] if not target else ["diff"],
            "branch": ["branch", "-a", "-v"],
            "show": ["show", "--stat", str(target) if target else "HEAD"],
            "remote": ["remote", "-v"],
            "blame": ["blame", "--date=short", "-L", f"1,{min(limit * 5, 200)}"],
            "stash-list": ["stash", "list"],
        }
        argv = [git, "-C", str(repository), *commands[subcommand]]
        if target and subcommand in {"diff", "blame"}:
            argv += ["--", str(target)]

        from localai.tools.shell import _stream_process

        code, output, truncated, timed_out = await _stream_process(
            argv,
            cwd=repository,
            timeout=30.0,
            max_bytes=context.config.safety.max_output_bytes,
            cancel=context.cancel,
        )
        if timed_out:
            return ToolResult.failure(f"git {subcommand} timed out")
        if code != 0:
            hint = (
                f"\n{repository} is not a Git repository."
                if "not a git repository" in output.lower()
                else ""
            )
            return ToolResult.failure(f"git {subcommand} failed: {output.strip()[:400]}{hint}")

        return ToolResult(
            content=f"$ git {subcommand}\n\n{output.strip() or '(no output)'}",
            untrusted=True,
            truncated=truncated,
            metadata={"subcommand": subcommand, "repository": str(repository), "exit_code": code},
        )
