"""PowerShell, POSIX shell and Python execution.

Security-sensitive module. Held to strict mypy settings and covered by
``tests/unit/test_shell_safety.py``.

Design decisions worth stating explicitly:

* **No shell string interpolation by us.** The command the user approved is the exact
  string handed to PowerShell. We never build a command by concatenating a model-
  supplied fragment into a template, because that is where injection lives.
* **``-Command`` with an explicit argument list**, not ``shell=True``. The command
  text is passed as a single argv element, so the Windows command-line parser is not
  given a second opportunity to reinterpret quoting. ``run_shell`` likewise passes the
  approved string as the single argument to ``bash -c``.
* **The full command is always displayed before execution** and recorded in the audit
  log, in every permission mode including bypass. A command that came from file
  content the model read is still shown in full before it runs.
* **Output is streamed and bounded.** A runaway command cannot fill memory.
* **On POSIX every command runs in its own process group**, and the whole group is
  signalled on timeout or cancellation. Otherwise ``sleep 600 &`` would outlive the
  shell, hold the output pipe open and leave the tool waiting forever.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
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
    # POSIX shells
    (
        re.compile(r"\brm\s+(?:-\S+\s+)*(?:-[a-z]*r|--recursive)", re.I),
        "recursive delete",
    ),
    (re.compile(r"\bmkfs(\.\w+)?\b|\bwipefs\b", re.I), "formats a volume"),
    (
        re.compile(r"\b(fdisk|sfdisk|gdisk|parted)\b|\bdd\b.*\bof=/dev/", re.I),
        "writes to a disk device or its partitions",
    ),
    (re.compile(r">\s*/dev/(sd|nvme|hd|vd|mmcblk)", re.I), "writes to a disk device"),
    (re.compile(r"\bshred\b", re.I), "securely wipes files"),
    (
        re.compile(r"\b(reboot|poweroff|halt)\b|\bsystemctl\s+(reboot|poweroff|halt)", re.I),
        "shuts down or restarts",
    ),
    (re.compile(r"\b(kill|pkill|killall)\s+.*-(9|kill)\b", re.I), "force-terminates processes"),
    (re.compile(r"\b(useradd|adduser|usermod|passwd)\b", re.I), "changes user accounts"),
    (
        re.compile(r"\b(chmod|chown|chgrp)\s+(?:-\S+\s+)*-[a-zA-Z]*R"),
        "changes file ownership or permissions recursively",
    ),
    (re.compile(r"\bsudo\b|\bdoas\b|\bpkexec\b", re.I), "runs with elevated privileges"),
    (re.compile(r"\bcrontab\s+-r\b", re.I), "removes the user's scheduled jobs"),
    (re.compile(r"\bgit\s+push\b.*(--force\b|\s-f\b)", re.I), "force-pushes Git history"),
)

#: Patterns indicating the command reaches the network. Used to enforce
#: ``privacy.network_disabled`` and to display the network indicator.
NETWORK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\binvoke-webrequest\b|\biwr\b|\binvoke-restmethod\b|\birm\b", re.I),
    re.compile(r"\bcurl\b|\bwget\b|\bnet\s+use\b", re.I),
    re.compile(r"\bstart-bitstransfer\b|\bdownload(file|string)\b", re.I),
    re.compile(r"\btest-netconnection\b|\bresolve-dnsname\b", re.I),
    re.compile(r"\b(ssh|scp|sftp|rsync|nc|ncat|telnet|ftp)\s", re.I),
    re.compile(r"\bgit\s+(clone|fetch|pull|push)\b", re.I),
    re.compile(r"\b(pip3?|npm|pnpm|yarn|cargo|gem)\s+(install|add)\b", re.I),
    re.compile(
        r"\b(apt|apt-get|dnf|yum|pacman|zypper|apk|brew)\s+(install|update|upgrade|-S)", re.I
    ),
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


def find_posix_shell() -> Path | None:
    """Locate a POSIX shell for ``run_shell``: bash if present, else sh.

    Returns ``None`` on Windows even if a ``bash.exe`` is on PATH -- that is usually
    the WSL launcher, which runs commands in a different filesystem namespace from
    the one the permissions engine evaluated paths against.
    """
    if os.name == "nt":
        return None
    for executable in ("bash", "sh"):
        if located := shutil.which(executable):
            return Path(located)
    return None


def _signal_process(process: asyncio.subprocess.Process, *, kill: bool = False) -> None:
    """Terminate (or kill) a process and, on POSIX, every process in its group."""
    try:
        if sys.platform != "win32":  # sys.platform, not os.name: the type checker narrows it
            os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
        elif kill:
            process.kill()
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError):
        pass


async def _stream_process(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float,
    max_bytes: int,
    cancel: asyncio.Event | None,
    on_output: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int | None, str, bool, bool]:
    """Run a process, streaming stdout+stderr. Returns (code, output, truncated, timed_out).

    stderr is merged into stdout so the model sees errors in the order they occurred
    relative to normal output -- interleaving matters for diagnosing a failure.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,  # never let a command block on input
            # Its own process group, so a timeout or cancel reaches every child.
            start_new_session=sys.platform != "win32",
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
            _signal_process(process)

    pump_task = asyncio.create_task(pump())
    cancel_task = asyncio.create_task(watch_cancel())
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        _signal_process(process)
        try:
            # Give it a moment to exit cleanly before escalating to a hard kill.
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            _signal_process(process, kill=True)
            await process.wait()
    finally:
        cancel_task.cancel()
        try:
            # A background child (``cmd &``) can keep the pipe open after the main
            # process exits. Allow it a moment, then end the group so we return.
            await asyncio.wait_for(asyncio.shield(pump_task), timeout=2.0)
        except TimeoutError:
            _signal_process(process, kill=True)
            pump_task.cancel()
        except Exception:  # noqa: S110 - the pump's own errors are not ours to raise
            pass
        await asyncio.gather(pump_task, cancel_task, return_exceptions=True)

    raw = b"".join(chunks)
    # Windows console output is frequently UTF-16 or cp1252; decode defensively so
    # non-ASCII filenames survive into the model's context.
    text = raw.decode("utf-8", errors="replace")
    return process.returncode, text, truncated, timed_out


class RunPowerShell(Tool):
    """Run a command through PowerShell. :class:`RunShell` reuses all of this."""

    name = "run_powershell"
    #: Prefix of the confirmation string, e.g. ``powershell: <command>``.
    label = "powershell"
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
        return f"{self.label}: {command}{suffix}"

    def find_interpreter(self) -> Path | None:
        return find_powershell()

    def missing_interpreter_message(self) -> str:
        return (
            "PowerShell was not found. Install PowerShell 7 (pwsh) or ensure "
            "powershell.exe is on PATH."
            + ("" if os.name == "nt" else " On Linux and macOS, run_shell needs no install.")
        )

    def build_argv(self, interpreter: Path, command: str) -> list[str]:
        return [
            str(interpreter),
            "-NoProfile",  # a user profile could redefine cmdlets used by the command
            "-NonInteractive",  # a prompt would hang forever with stdin at /dev/null
            "-ExecutionPolicy",
            "Bypass",  # scoped to this process only; nothing persists
            "-Command",
            command,
        ]

    def build_env(self) -> dict[str, str] | None:
        """Environment for the child process; ``None`` inherits ours unchanged."""
        return None

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        command = str(arguments["command"])
        shell = self.find_interpreter()
        if shell is None:
            return ToolResult.failure(self.missing_interpreter_message())

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
        argv = self.build_argv(shell, command)

        code, output, truncated, timed_out = await _stream_process(
            argv,
            cwd=cwd,
            timeout=timeout,
            max_bytes=context.config.safety.max_output_bytes,
            cancel=context.cancel,
            env=self.build_env(),
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


class RunShell(RunPowerShell):
    """Run a command through bash (or sh) on Linux, macOS and other Unix systems.

    Identical in every safety-relevant respect to ``run_powershell``: same risk,
    same verbatim confirmation string, same destructive and network classification,
    same bounded output. Only the interpreter and its argv differ.
    """

    name = "run_shell"
    label = "shell"
    description = (
        "Run a shell command with bash (or sh if bash is absent) and return its combined "
        "output. Use this on Linux and macOS for querying system state, running build "
        "tools, and anything without a dedicated tool. Not available on Windows; use "
        "run_powershell there. The command is shown to the user before it runs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "working_directory": {"type": "string", "description": "Defaults to the session cwd."},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600},
        },
        "required": ["command"],
    }

    def find_interpreter(self) -> Path | None:
        return find_posix_shell()

    def missing_interpreter_message(self) -> str:
        if os.name == "nt":
            return "run_shell is not available on Windows. Use run_powershell instead."
        return "No POSIX shell was found: neither bash nor sh is on PATH."

    def build_argv(self, interpreter: Path, command: str) -> list[str]:
        if interpreter.name == "bash":
            # Startup files could redefine commands the user approved by name.
            return [str(interpreter), "--noprofile", "--norc", "-c", command]
        return [str(interpreter), "-c", command]

    def build_env(self) -> dict[str, str] | None:
        # Non-interactive bash sources $BASH_ENV, and sh sources $ENV, even with
        # --norc. Either could silently redefine the commands the user approved.
        return {k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV")}


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
