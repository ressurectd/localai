"""PowerShell and Python execution.

Security-sensitive module. Held to strict mypy settings and covered by
``tests/unit/test_shell_safety.py``.

Design decisions worth stating explicitly:

* **No shell string interpolation by us.** The command the user approved is the exact
  string handed to PowerShell. We never build a command by concatenating a model-
  supplied fragment into a template, because that is where injection lives.
* **``-Command`` with an explicit argument list**, not ``shell=True``. The command
  text is passed as a single argv element, so the Windows command-line parser is not
  given a second opportunity to reinterpret quoting.
* **The full command is always displayed before execution** and recorded in the audit
  log, in every permission mode including bypass. A command that came from file
  content the model read is still shown in full before it runs.
* **Output is streamed and bounded.** A runaway command cannot fill memory.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from localai.config.models import RiskLevel
from localai.tools.base import Tool, ToolContext, ToolResult

#: Patterns that mark a command as especially consequential. Matching does not block
#: anything -- the permissions engine decides that -- but it raises the command's
#: risk so that even Auto mode stops to confirm, and the UI flags it in red.
DESTRUCTIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bremove-item\b.*\s-recurse\b.*\s-force\b", re.I), "recursive forced delete"),
    (re.compile(r"\bformat-volume\b|\bformat\s+[a-z]:", re.I), "formats a volume"),
    (re.compile(r"\bclear-disk\b|\bdiskpart\b", re.I), "modifies disk partitions"),
    (re.compile(r"\bset-executionpolicy\b", re.I), "changes the PowerShell execution policy"),
    (
        re.compile(r"\bnew-itemproperty\b.*hklm:|reg\s+add\s+hklm", re.I),
        "writes to the machine registry",
    ),
    (
        re.compile(r"\bstop-computer\b|\brestart-computer\b|\bshutdown\b", re.I),
        "shuts down or restarts",
    ),
    (re.compile(r"\bstop-process\b.*\s-force\b", re.I), "force-terminates processes"),
    (re.compile(r"\bcipher\s+/w|\bsdelete\b", re.I), "securely wipes free space"),
    (re.compile(r"\bbcdedit\b|\bbootrec\b", re.I), "modifies boot configuration"),
    (re.compile(r"\bnet\s+user\b.*\s/add|\bnew-localuser\b", re.I), "creates a user account"),
    (
        re.compile(r"\bicacls\b.*\s/grant|\btakeown\b", re.I),
        "changes file ownership or permissions",
    ),
    (re.compile(r"\bvssadmin\b.*delete", re.I), "deletes volume shadow copies"),
)

#: Patterns indicating the command reaches the network. Used to enforce
#: ``privacy.network_disabled`` and to display the network indicator.
NETWORK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\binvoke-webrequest\b|\biwr\b|\binvoke-restmethod\b|\birm\b", re.I),
    re.compile(r"\bcurl\b|\bwget\b|\bnet\s+use\b", re.I),
    re.compile(r"\bstart-bitstransfer\b|\bdownload(file|string)\b", re.I),
    re.compile(r"\btest-netconnection\b|\bresolve-dnsname\b", re.I),
)


def classify_command(command: str) -> tuple[RiskLevel, list[str], bool]:
    """Return ``(risk, reasons, touches_network)`` for a command string.

    Called before the permissions engine sees the request, so that a destructive
    command is evaluated at destructive risk rather than at the tool's base risk.
    """
    reasons = [reason for pattern, reason in DESTRUCTIVE_PATTERNS if pattern.search(command)]
    network = any(pattern.search(command) for pattern in NETWORK_PATTERNS)
    risk = RiskLevel.DESTRUCTIVE if reasons else RiskLevel.EXECUTE
    return risk, reasons, network


def find_powershell() -> Path | None:
    """Locate PowerShell, preferring cross-platform ``pwsh`` over Windows PowerShell.

    Resolved to an absolute path so execution does not depend on ``PATH`` ordering
    at call time -- a directory prepended to PATH must not silently change which
    interpreter runs.
    """
    for executable in ("pwsh", "powershell"):
        if located := shutil.which(executable):
            return Path(located)
    if os.name == "nt":
        fallback = Path(os.environ.get("SystemRoot", "C:/Windows")) / (
            "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        if fallback.exists():
            return fallback
    return None


async def _stream_process(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float,
    max_bytes: int,
    cancel: asyncio.Event | None,
    on_output: Callable[[str], None] | None = None,
) -> tuple[int | None, str, bool, bool]:
    """Run a process, streaming stdout+stderr. Returns (code, output, truncated, timed_out).

    stderr is merged into stdout so the model sees errors in the order they occurred
    relative to normal output -- interleaving matters for diagnosing a failure.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,  # never let a command block on input
        )
    except FileNotFoundError:
        return None, f"executable not found: {argv[0]}", False, False
    except OSError as exc:
        return None, f"cannot start process: {exc}", False, False

    chunks: list[bytes] = []
    collected = 0
    truncated = False

    async def pump() -> None:
        nonlocal collected, truncated
        assert process.stdout is not None
        while True:
            chunk = await process.stdout.read(8192)
            if not chunk:
                return
            if collected < max_bytes:
                chunks.append(chunk)
                collected += len(chunk)
                if on_output is not None:
                    on_output(chunk.decode("utf-8", errors="replace"))
            else:
                truncated = True  # keep draining so the process does not block on a full pipe

    async def watch_cancel() -> None:
        """Terminate the process if the user cancels."""
        if cancel is None:
            await asyncio.Future()  # never completes
        else:
            await cancel.wait()
            with __import__("contextlib").suppress(ProcessLookupError):
                process.terminate()

    pump_task = asyncio.create_task(pump())
    cancel_task = asyncio.create_task(watch_cancel())
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        try:
            process.terminate()
            # Give it a moment to exit cleanly before escalating to a hard kill.
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except (TimeoutError, ProcessLookupError):
            with __import__("contextlib").suppress(ProcessLookupError):
                process.kill()
    finally:
        cancel_task.cancel()
        await asyncio.gather(pump_task, cancel_task, return_exceptions=True)

    raw = b"".join(chunks)
    # Windows console output is frequently UTF-16 or cp1252; decode defensively so
    # non-ASCII filenames survive into the model's context.
    text = raw.decode("utf-8", errors="replace")
    return process.returncode, text, truncated, timed_out


class RunPowerShell(Tool):
    name = "run_powershell"
    description = (
        "Run a PowerShell command and return its combined output. Use this for Windows "
        "administration, querying system state, and anything without a dedicated tool. "
        "The command is shown to the user before it runs."
    )
    category = "shell"
    risk = RiskLevel.EXECUTE
    mutating = True
    returns_untrusted_content = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The PowerShell command to run."},
            "working_directory": {"type": "string", "description": "Defaults to the session cwd."},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600},
        },
        "required": ["command"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        directory = arguments.get("working_directory")
        return [Path(str(directory))] if isinstance(directory, str) and directory else []

    def describe_call(self, arguments: dict[str, Any]) -> str:
        """Show the command verbatim -- never a summary.

        The user is being asked to authorise this exact string, so paraphrasing it
        would defeat the point of asking.
        """
        command = str(arguments.get("command", ""))
        _, reasons, network = classify_command(command)
        annotations = []
        if reasons:
            annotations.append("DESTRUCTIVE: " + "; ".join(reasons))
        if network:
            annotations.append("makes a network request")
        suffix = f"  [{' | '.join(annotations)}]" if annotations else ""
        return f"powershell: {command}{suffix}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        command = str(arguments["command"])
        shell = find_powershell()
        if shell is None:
            return ToolResult.failure(
                "PowerShell was not found. Install PowerShell 7 (pwsh) or ensure "
                "powershell.exe is on PATH."
            )

        cwd = Path(str(arguments.get("working_directory") or context.cwd))
        if not cwd.is_dir():
            return ToolResult.failure(f"working directory {cwd} does not exist")

        risk, reasons, network = classify_command(command)
        if network and context.config.privacy.network_disabled:
            return ToolResult.failure(
                "this command appears to make a network request and network-disabled mode "
                "is on. Turn it off with '/mode network on' if that is intended."
            )
        if context.dry_run:
            return ToolResult(
                content=f"[dry run] would run in {cwd}:\n{command}",
                flags=["dry_run"],
                metadata={"command": command, "risk": risk.value, "reasons": reasons},
            )

        timeout = float(arguments.get("timeout_seconds") or context.config.safety.shell_timeout_s)
        argv = [
            str(shell),
            "-NoProfile",  # a user profile could redefine cmdlets used by the command
            "-NonInteractive",  # a prompt would hang forever with stdin at /dev/null
            "-ExecutionPolicy",
            "Bypass",  # scoped to this process only; nothing persists
            "-Command",
            command,
        ]

        code, output, truncated, timed_out = await _stream_process(
            argv,
            cwd=cwd,
            timeout=timeout,
            max_bytes=context.config.safety.max_output_bytes,
            cancel=context.cancel,
        )

        if timed_out:
            return ToolResult(
                ok=False,
                error=f"timed out after {timeout:.0f}s",
                content=f"Command timed out after {timeout:.0f}s and was terminated.\n\n{output}",
                untrusted=True,
                flags=["timeout"],
                metadata={"command": command, "timeout_s": timeout, "partial_output": True},
            )

        status = "succeeded" if code == 0 else f"failed with exit code {code}"
        body = output.strip() or "(no output)"
        return ToolResult(
            ok=code == 0,
            content=f"$ {command}\n[{status}]\n\n{body}",
            error=None if code == 0 else f"exit code {code}",
            untrusted=True,
            truncated=truncated,
            flags=["destructive"] if reasons else [],
            metadata={
                "command": command,
                "exit_code": code,
                "cwd": str(cwd),
                "risk": risk.value,
                "destructive_reasons": reasons,
                "network": network,
            },
        )


class RunPython(Tool):
    name = "run_python"
    description = (
        "Run a Python script and return its output. The script runs in a separate "
        "process using the same interpreter as this application. Use it for "
        "calculations, data processing and file analysis."
    )
    category = "shell"
    risk = RiskLevel.EXECUTE
    mutating = True
    returns_untrusted_content = True
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source to execute."},
            "working_directory": {"type": "string"},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600},
        },
        "required": ["code"],
    }

    def describe_call(self, arguments: dict[str, Any]) -> str:
        code = str(arguments.get("code", ""))
        first = next((line for line in code.splitlines() if line.strip()), "")
        lines = len(code.splitlines())
        return f"python ({lines} lines): {first[:70]}" + ("..." if lines > 1 else "")

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        code = str(arguments["code"])
        cwd = Path(str(arguments.get("working_directory") or context.cwd))
        if not cwd.is_dir():
            return ToolResult.failure(f"working directory {cwd} does not exist")
        if context.dry_run:
            return ToolResult(content=f"[dry run] would run Python:\n{code}", flags=["dry_run"])

        # A temporary file rather than `-c`: it keeps tracebacks readable (real line
        # numbers, a real filename) and sidesteps command-line length limits.
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".py",
            prefix="localai-",
            delete=False,
            dir=context.paths.tool_output_dir,
        )
        script = Path(handle.name)
        try:
            with handle:
                handle.write(code)

            timeout = float(
                arguments.get("timeout_seconds") or context.config.safety.shell_timeout_s
            )
            code_returned, output, truncated, timed_out = await _stream_process(
                [sys.executable, "-I", str(script)],  # -I: isolated, ignores env and user site
                cwd=cwd,
                timeout=timeout,
                max_bytes=context.config.safety.max_output_bytes,
                cancel=context.cancel,
            )
        finally:
            script.unlink(missing_ok=True)

        if timed_out:
            return ToolResult(
                ok=False,
                error=f"timed out after {timeout:.0f}s",
                content=f"Script timed out after {timeout:.0f}s.\n\n{output}",
                untrusted=True,
                flags=["timeout"],
            )

        status = "succeeded" if code_returned == 0 else f"exited with code {code_returned}"
        return ToolResult(
            ok=code_returned == 0,
            content=f"[python {status}]\n\n{output.strip() or '(no output)'}",
            error=None if code_returned == 0 else f"exit code {code_returned}",
            untrusted=True,
            truncated=truncated,
            metadata={"exit_code": code_returned, "cwd": str(cwd), "lines": len(code.splitlines())},
        )
