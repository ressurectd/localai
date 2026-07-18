"""Structured logging configuration.

Two rules that matter for machine consumption:

1. **Logs go to stderr, never stdout.** stdout is reserved for JSON output, so a
   caller can safely do ``localai usage report --json | jq`` without a stray log line
   corrupting the parse.
2. **JSON mode is available** via ``--log-format json`` for callers that want to
   consume the log itself.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Anything attached via `extra=` is included, which is how callers add
        # structured context without string-formatting it into the message.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str)


_STANDARD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def configure(
    *,
    level: str = "WARNING",
    log_file: Path | None = None,
    json_format: bool = False,
    quiet: bool = False,
) -> None:
    """Install handlers. Safe to call more than once; existing handlers are replaced."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if not quiet:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(getattr(logging, level.upper(), logging.WARNING))
        console.setFormatter(
            JsonFormatter()
            if json_format
            else logging.Formatter("%(levelname)-7s %(name)s: %(message)s")
        )
        root.addHandler(console)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            # The file log is always DEBUG and always JSON: when something goes wrong
            # the user is asked for this file, and it should contain everything.
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(JsonFormatter())
            root.addHandler(file_handler)
        except OSError:
            # A read-only or full disk must not prevent the application from starting.
            pass

    # httpx logs every request at INFO, which would narrate each streaming chunk.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
