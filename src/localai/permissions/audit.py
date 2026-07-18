"""Append-only audit logging.

Every permission decision and every tool execution is recorded, in every mode
including bypass. Two sinks are written:

* ``logs/audit.jsonl`` -- append-only newline-delimited JSON, easy to tail, grep or
  ship to an external tool.
* the ``audit_log`` SQLite table -- queryable, and what ``localai logs`` reads.

The audit logger is deliberately *not* exposed as a tool and its files sit inside
:meth:`AppPaths.protected_paths`, so no model-initiated action can disable it or
edit history. A write failure is reported but never aborts the operation being
audited, because losing the operation is worse than losing one log line -- the
failure itself is recorded to the application log.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from localai.permissions.engine import Caller, Decision, PermissionRequest

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AuditEntry:
    """One auditable event."""

    ts: float
    interface: str
    client_id: str
    tool: str
    effect: str
    stage: str
    risk: str
    reason: str
    outcome: str
    """``pending`` | ``executed`` | ``denied`` | ``cancelled`` | ``failed`` | ``dry_run``"""

    matched_rule: str | None = None
    command: str | None = None
    paths: list[str] = field(default_factory=list)
    arguments: dict[str, Any] = field(default_factory=dict)
    conversation_id: str | None = None
    duration_ms: float | None = None
    error: str | None = None
    sensitive: list[str] = field(default_factory=list)
    injection_signals: list[str] = field(default_factory=list)
    confirmed_by_user: bool | None = None
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditLogger:
    """Writes :class:`AuditEntry` records to JSONL and SQLite."""

    def __init__(self, audit_path: Path, store: Any | None = None) -> None:
        """``store`` is an optional :class:`~localai.storage.audit.AuditStore`.

        It is typed loosely to keep this module free of a storage import cycle;
        anything with a ``record(entry)`` method works, which also makes the
        logger trivial to fake in tests.
        """
        self.path = audit_path
        self.store = store
        self._failed_writes = 0

    def record(self, entry: AuditEntry) -> AuditEntry:
        """Persist one entry. Never raises."""
        if not entry.id:
            entry.id = f"aud_{int(entry.ts * 1000):x}_{os.getpid():x}_{self._failed_writes:x}"
        self._append_jsonl(entry)
        if self.store is not None:
            try:
                self.store.record(entry)
            except Exception:
                log.exception("audit database write failed; JSONL copy retained")
        return entry

    def record_decision(
        self,
        request: PermissionRequest,
        decision: Decision,
        *,
        outcome: str = "pending",
        conversation_id: str | None = None,
        confirmed_by_user: bool | None = None,
    ) -> AuditEntry:
        """Convenience constructor from a request/decision pair."""
        return self.record(
            AuditEntry(
                ts=time.time(),
                interface=request.caller.interface.value,
                client_id=request.caller.client_id,
                tool=request.tool,
                effect=decision.effect.value,
                stage=decision.stage,
                risk=decision.risk.value,
                reason=decision.reason,
                outcome=outcome,
                matched_rule=decision.matched_rule_id,
                command=request.command,
                paths=[str(p) for p in request.paths],
                arguments=_redact(request.arguments),
                conversation_id=conversation_id,
                sensitive=[m.kind.value for m in decision.sensitive_matches],
                confirmed_by_user=confirmed_by_user,
            )
        )

    def record_execution(
        self,
        request: PermissionRequest,
        decision: Decision,
        *,
        outcome: str,
        duration_ms: float,
        error: str | None = None,
        injection_signals: list[str] | None = None,
        conversation_id: str | None = None,
    ) -> AuditEntry:
        """Record the result of an action that was permitted to run."""
        return self.record(
            AuditEntry(
                ts=time.time(),
                interface=request.caller.interface.value,
                client_id=request.caller.client_id,
                tool=request.tool,
                effect=decision.effect.value,
                stage=decision.stage,
                risk=decision.risk.value,
                reason=decision.reason,
                outcome=outcome,
                matched_rule=decision.matched_rule_id,
                command=request.command,
                paths=[str(p) for p in request.paths],
                arguments=_redact(request.arguments),
                conversation_id=conversation_id,
                duration_ms=duration_ms,
                error=error,
                injection_signals=injection_signals or [],
            )
        )

    def record_event(self, event: str, caller: Caller, **details: Any) -> AuditEntry:
        """Record a non-tool event: a mode change, a kill switch toggle, a grant."""
        return self.record(
            AuditEntry(
                ts=time.time(),
                interface=caller.interface.value,
                client_id=caller.client_id,
                tool=f"@{event}",
                effect="event",
                stage="event",
                risk="read",
                reason=str(details.get("reason", event)),
                outcome="executed",
                arguments=_redact(details),
            )
        )

    def _append_jsonl(self, entry: AuditEntry) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry.to_dict(), ensure_ascii=False, default=str) + "\n")
        except OSError:
            self._failed_writes += 1
            log.exception("could not append to audit log at %s", self.path)


#: Argument keys whose values are replaced before being written to the audit log.
_REDACT_KEYS = frozenset(
    {"password", "passwd", "secret", "token", "api_key", "apikey", "credential"}
)


def _redact(arguments: dict[str, Any]) -> dict[str, Any]:
    """Redact obviously secret-bearing argument values and cap size.

    The audit log records *what was attempted*, so arguments matter -- but a tool
    argument named ``password`` should not be written to a file in the clear, and a
    500 KB file body would make the log unusable.
    """
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        if key.lower() in _REDACT_KEYS:
            out[key] = "<redacted>"
        elif isinstance(value, str) and len(value) > 2000:
            out[key] = value[:2000] + f"... <truncated, {len(value)} chars total>"
        else:
            out[key] = value
    return out
