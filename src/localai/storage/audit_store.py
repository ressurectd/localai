"""SQLite sink for audit entries, and queries over audit history.

Separated from :mod:`localai.permissions.audit` so the permissions package has no
dependency on storage: the audit logger accepts any object with a ``record`` method,
which keeps the security-critical code testable without a database.
"""

from __future__ import annotations

import json
from typing import Any

from localai.permissions.audit import AuditEntry
from localai.storage.db import Database


class AuditStore:
    """Persists and queries :class:`AuditEntry` records."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, entry: AuditEntry) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO audit_log (id, ts, interface, client_id, tool, effect, stage,"
            " risk, reason, outcome, matched_rule, command, paths_json, arguments_json,"
            " conversation_id, duration_ms, error, sensitive_json, injection_json,"
            " confirmed_by_user) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entry.id,
                entry.ts,
                entry.interface,
                entry.client_id,
                entry.tool,
                entry.effect,
                entry.stage,
                entry.risk,
                entry.reason,
                entry.outcome,
                entry.matched_rule,
                entry.command,
                json.dumps(entry.paths),
                json.dumps(entry.arguments, default=str),
                entry.conversation_id,
                entry.duration_ms,
                entry.error,
                json.dumps(entry.sensitive),
                json.dumps(entry.injection_signals),
                None if entry.confirmed_by_user is None else int(entry.confirmed_by_user),
            ),
        )

    def recent(
        self,
        *,
        limit: int = 100,
        tool: str | None = None,
        effect: str | None = None,
        conversation_id: str | None = None,
        since: float | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for condition, value in (
            ("tool = ?", tool),
            ("effect = ?", effect),
            ("conversation_id = ?", conversation_id),
            ("ts >= ?", since),
        ):
            if value is not None:
                clauses.append(condition)
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.query(
            f"SELECT * FROM audit_log {where} ORDER BY ts DESC LIMIT ?", [*params, limit]
        )
        return [self._row_to_dict(r) for r in rows]

    def summary(self, *, since: float | None = None) -> dict[str, Any]:
        """Counts by effect and by tool, for the /logs view and doctor."""
        where, params = ("WHERE ts >= ?", [since]) if since else ("", [])
        by_effect = self.db.query(
            f"SELECT effect, COUNT(*) AS n FROM audit_log {where} GROUP BY effect", params
        )
        by_tool = self.db.query(
            f"SELECT tool, COUNT(*) AS n FROM audit_log {where} GROUP BY tool ORDER BY n DESC"
            " LIMIT 20",
            params,
        )
        denied = self.db.query(
            f"SELECT tool, reason, ts FROM audit_log {where}"
            + (" AND " if where else " WHERE ")
            + "effect = 'deny' ORDER BY ts DESC LIMIT 10",
            params,
        )
        return {
            "by_effect": {r["effect"]: int(r["n"]) for r in by_effect},
            "by_tool": {r["tool"]: int(r["n"]) for r in by_tool},
            "recent_denials": [dict(r) for r in denied],
        }

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        for column, key in (
            ("paths_json", "paths"),
            ("arguments_json", "arguments"),
            ("sensitive_json", "sensitive"),
            ("injection_json", "injection_signals"),
        ):
            data[key] = json.loads(data.pop(column) or "[]")
        return data
