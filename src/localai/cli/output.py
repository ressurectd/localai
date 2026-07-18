"""Output helpers shared by every CLI command.

The contract external agents depend on:

* ``--json`` puts **only** JSON on stdout. Logs, progress and warnings go to stderr.
* every JSON document carries ``schema`` and ``schema_version`` so a consumer can
  detect a format change instead of silently misreading it.
* exit codes are meaningful (see :mod:`localai.errors`).
* ``--no-color`` and a non-TTY stdout both disable ANSI.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, NoReturn, TextIO

from localai.errors import LocalAIError
from localai.version import CLI_SCHEMA_VERSION


@dataclass(slots=True)
class OutputMode:
    """How this invocation should render output."""

    json: bool = False
    color: bool = True
    quiet: bool = False
    verbose: bool = False

    @classmethod
    def from_args(cls, args: Any) -> OutputMode:
        # NO_COLOR is the de-facto cross-tool standard; honouring it is free.
        color = (
            not getattr(args, "no_color", False)
            and not os.environ.get("NO_COLOR")
            and sys.stdout.isatty()
        )
        return cls(
            json=bool(getattr(args, "json", False)),
            color=color,
            quiet=bool(getattr(args, "quiet", False)),
            verbose=bool(getattr(args, "verbose", False)),
        )


# Minimal ANSI. Rich is available but a dependency-free path keeps `--json` and
# piped output completely predictable, which is what scripts need.
_STYLES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "reset": "\033[0m",
}


def style(text: str, *names: str, enabled: bool = True) -> str:
    if not enabled or not names:
        return text
    return "".join(_STYLES.get(n, "") for n in names) + text + _STYLES["reset"]


def emit_json(payload: dict[str, Any], *, schema: str, stream: TextIO | None = None) -> None:
    """Write a JSON document to stdout with schema identification."""
    document = {"schema": schema, "schema_version": CLI_SCHEMA_VERSION, **payload}
    print(json.dumps(document, indent=2, default=str), file=stream or sys.stdout)


def emit_ndjson(record: dict[str, Any], *, stream: TextIO | None = None) -> None:
    """Write one newline-delimited JSON record and flush.

    Flushing per record matters for streaming: a consumer reading line by line
    should see each event as it happens, not when the process exits.
    """
    target = stream or sys.stdout
    print(json.dumps(record, default=str), file=target, flush=True)


def info(message: str, mode: OutputMode) -> None:
    """Human-facing note. Suppressed in JSON and quiet modes."""
    if not mode.json and not mode.quiet:
        print(message)


def warn(message: str, mode: OutputMode) -> None:
    """Warning. Always to stderr so it never contaminates JSON stdout."""
    if not mode.quiet:
        print(style(f"warning: {message}", "yellow", enabled=mode.color), file=sys.stderr)


def fail(error: LocalAIError | str, mode: OutputMode, *, exit_code: int | None = None) -> NoReturn:
    """Report an error and exit with a meaningful status.

    In JSON mode the error envelope goes to stdout so a consumer gets a parseable
    result either way; the human-readable form goes to stderr.
    """
    if isinstance(error, LocalAIError):
        payload = error.to_dict()
        status = exit_code if exit_code is not None else error.exit_code
        text = error.message
        remediation = error.remediation
    else:
        payload = {"code": "error", "message": str(error), "remediation": "", "details": {}}
        status = exit_code if exit_code is not None else 1
        text = str(error)
        remediation = ""

    if mode.json:
        emit_json({"ok": False, "error": payload}, schema="localai.error/1")
    else:
        print(style(f"error: {text}", "red", enabled=mode.color), file=sys.stderr)
        if remediation:
            print(style(f"  -> {remediation}", "dim", enabled=mode.color), file=sys.stderr)
    raise SystemExit(status)


def table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], *, mode: OutputMode) -> str:
    """Render an aligned plain-text table.

    ``columns`` is a list of ``(key, heading)`` pairs. Values are stringified and
    columns sized to content, so Unicode filenames and long model names stay aligned.
    """
    if not rows:
        return "(none)"

    widths = {
        key: max(len(heading), max(len(str(row.get(key, ""))) for row in rows))
        for key, heading in columns
    }
    header = "  ".join(heading.ljust(widths[key]) for key, heading in columns)
    separator = "  ".join("-" * widths[key] for key, _ in columns)
    body = [
        "  ".join(str(row.get(key, "")).ljust(widths[key]) for key, _ in columns) for row in rows
    ]
    return "\n".join([style(header, "bold", enabled=mode.color), separator, *body])


def humanise_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def sparkline(values: list[float]) -> str:
    """Render a compact bar chart using block characters.

    Used for the daily usage series. Eight levels is what the block characters
    give us; anything finer would not be visible at terminal resolution.
    """
    if not values or all(v == 0 for v in values):
        return "▁" * len(values)
    blocks = "▁▂▃▄▅▆▇█"
    peak = max(values)
    return "".join(blocks[min(int(v / peak * (len(blocks) - 1)), len(blocks) - 1)] for v in values)
