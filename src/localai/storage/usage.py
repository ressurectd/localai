"""Usage accounting.

Every figure this module returns carries provenance. ``UsageTotals.exactness`` tells
a caller whether the numbers came entirely from Ollama, entirely from our own
estimates, or a mixture -- and the CLI and TUI both render that distinction rather
than presenting a single confident number. Silently summing reported and estimated
counts into one figure would be the easy thing to do and would be dishonest.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from localai.domain.messages import Usage, new_id
from localai.storage.db import Database


class Period(StrEnum):
    """Reporting windows. Boundaries are computed in the user's local timezone."""

    SESSION = "session"
    TODAY = "today"
    SEVEN_DAYS = "7d"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"
    ALL = "all"


def period_bounds(period: Period, *, now: float | None = None) -> tuple[float | None, float | None]:
    """Return ``(start, end)`` epoch seconds for ``period``; ``None`` means unbounded.

    Calendar periods (week/month/year) snap to local calendar boundaries, whereas
    ``7d`` is a rolling window. Conflating the two is a common source of confusing
    usage reports, so both are offered explicitly.
    """
    current = datetime.fromtimestamp(now if now is not None else time.time())
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)

    match period:
        case Period.ALL | Period.SESSION:
            return None, None
        case Period.TODAY:
            return midnight.timestamp(), None
        case Period.SEVEN_DAYS:
            return (current - timedelta(days=7)).timestamp(), None
        case Period.WEEK:  # ISO week, starting Monday
            return (midnight - timedelta(days=midnight.weekday())).timestamp(), None
        case Period.MONTH:
            return midnight.replace(day=1).timestamp(), None
        case Period.YEAR:
            return midnight.replace(month=1, day=1).timestamp(), None
    return None, None


@dataclass(slots=True)
class UsageTotals:
    """Aggregated usage with explicit provenance."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0
    message_count: int = 0
    tool_calls: int = 0
    generations: int = 0
    total_duration_s: float = 0.0
    energy_wh_estimate: float = 0.0
    reported_generations: int = 0
    estimated_generations: int = 0
    unknown_generations: int = 0
    models: dict[str, int] = field(default_factory=dict)

    @property
    def exactness(self) -> str:
        """``exact`` | ``mixed`` | ``estimated`` | ``unknown`` | ``empty``."""
        if self.generations == 0:
            return "empty"
        if self.reported_generations == self.generations:
            return "exact"
        if self.reported_generations == 0:
            return "unknown" if self.unknown_generations == self.generations else "estimated"
        return "mixed"

    @property
    def tokens_per_second(self) -> float | None:
        if self.total_duration_s <= 0 or self.completion_tokens <= 0:
            return None
        return self.completion_tokens / self.total_duration_s

    def format_tokens(self, value: int) -> str:
        """Render a token count with a marker when it is not exact.

        ``~`` prefixes an estimate and ``?`` marks a total that includes generations
        Ollama gave us nothing for. This is what makes the honesty requirement
        visible at every call site rather than buried in a docstring.
        """
        match self.exactness:
            case "exact":
                return f"{value:,}"
            case "unknown":
                return f"{value:,}?"
            case _:
                return f"~{value:,}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "thinking_tokens": self.thinking_tokens,
            "total_tokens": self.total_tokens,
            "message_count": self.message_count,
            "tool_calls": self.tool_calls,
            "generations": self.generations,
            "total_duration_s": round(self.total_duration_s, 3),
            "tokens_per_second": (
                round(tps, 2) if (tps := self.tokens_per_second) is not None else None
            ),
            "energy_wh_estimate": round(self.energy_wh_estimate, 4),
            "energy_is_estimate": True,
            "token_exactness": self.exactness,
            "generations_by_source": {
                "reported": self.reported_generations,
                "estimated": self.estimated_generations,
                "unknown": self.unknown_generations,
            },
            "models": self.models,
        }


class UsageStore:
    """Writes and aggregates usage records."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        usage: Usage,
        *,
        model: str,
        conversation_id: str | None = None,
        workspace: str = "",
        tool_calls: int = 0,
        message_count: int = 1,
        assumed_watts: float | None = None,
    ) -> str:
        """Persist one generation's usage. Returns the record id."""
        record_id = new_id("use")
        self.db.execute(
            "INSERT INTO usage_records (id, conversation_id, ts, model, workspace, prompt_tokens,"
            " completion_tokens, thinking_tokens, total_tokens, token_source,"
            " thinking_token_source, total_duration_ns, load_duration_ns,"
            " prompt_eval_duration_ns, eval_duration_ns, tokens_per_second, tool_calls,"
            " message_count, energy_wh_estimate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record_id,
                conversation_id,
                time.time(),
                model,
                workspace,
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.thinking_tokens,
                usage.total_tokens,
                usage.token_source.value,
                usage.thinking_token_source.value,
                usage.total_duration_ns,
                usage.load_duration_ns,
                usage.prompt_eval_duration_ns,
                usage.eval_duration_ns,
                usage.tokens_per_second,
                tool_calls,
                message_count,
                usage.energy_wh_estimate(assumed_watts) if assumed_watts else None,
            ),
        )
        return record_id

    def totals(
        self,
        period: Period = Period.ALL,
        *,
        model: str | None = None,
        workspace: str | None = None,
        conversation_id: str | None = None,
        since: float | None = None,
    ) -> UsageTotals:
        """Aggregate usage over a window with optional filters."""
        start, end = period_bounds(period)
        if since is not None:
            start = since if start is None else max(start, since)

        clauses: list[str] = []
        params: list[Any] = []
        for condition, value in (
            ("ts >= ?", start),
            ("ts < ?", end),
            ("model = ?", model),
            ("workspace = ?", workspace),
            ("conversation_id = ?", conversation_id),
        ):
            if value is not None:
                clauses.append(condition)
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        row = self.db.query_one(
            "SELECT COALESCE(SUM(prompt_tokens),0) AS prompt,"
            " COALESCE(SUM(completion_tokens),0) AS completion,"
            " COALESCE(SUM(thinking_tokens),0) AS thinking,"
            " COALESCE(SUM(total_tokens),0) AS total,"
            " COALESCE(SUM(message_count),0) AS messages,"
            " COALESCE(SUM(tool_calls),0) AS tools,"
            " COUNT(*) AS generations,"
            " COALESCE(SUM(total_duration_ns),0) AS duration,"
            " COALESCE(SUM(energy_wh_estimate),0) AS energy,"
            " COALESCE(SUM(token_source = 'reported'),0) AS reported,"
            " COALESCE(SUM(token_source = 'estimated'),0) AS estimated,"
            " COALESCE(SUM(token_source = 'unknown'),0) AS unknown"
            f" FROM usage_records {where}",
            params,
        )
        assert row is not None  # aggregate queries always return exactly one row

        model_rows = self.db.query(
            f"SELECT model, COUNT(*) AS n FROM usage_records {where} GROUP BY model ORDER BY n DESC",
            params,
        )
        return UsageTotals(
            prompt_tokens=int(row["prompt"]),
            completion_tokens=int(row["completion"]),
            thinking_tokens=int(row["thinking"]),
            total_tokens=int(row["total"]),
            message_count=int(row["messages"]),
            tool_calls=int(row["tools"]),
            generations=int(row["generations"]),
            total_duration_s=int(row["duration"]) / 1e9,
            energy_wh_estimate=float(row["energy"] or 0.0),
            reported_generations=int(row["reported"]),
            estimated_generations=int(row["estimated"]),
            unknown_generations=int(row["unknown"]),
            models={r["model"]: int(r["n"]) for r in model_rows},
        )

    def by_model(self, period: Period = Period.ALL) -> list[dict[str, Any]]:
        return self._grouped("model", period)

    def by_workspace(self, period: Period = Period.ALL) -> list[dict[str, Any]]:
        return self._grouped("workspace", period)

    def by_conversation(
        self, period: Period = Period.ALL, *, limit: int = 25
    ) -> list[dict[str, Any]]:
        start, _ = period_bounds(period)
        where, params = ("WHERE u.ts >= ?", [start]) if start else ("", [])
        rows = self.db.query(
            "SELECT u.conversation_id, COALESCE(c.title,'(deleted)') AS title,"
            " SUM(u.total_tokens) AS tokens, COUNT(*) AS generations,"
            " SUM(u.total_duration_ns)/1e9 AS seconds,"
            " MIN(u.token_source = 'reported') AS all_reported"
            " FROM usage_records u LEFT JOIN conversations c ON c.id = u.conversation_id"
            f" {where} GROUP BY u.conversation_id ORDER BY tokens DESC LIMIT ?",
            [*params, limit],
        )
        return [
            {
                "conversation_id": r["conversation_id"],
                "title": r["title"],
                "total_tokens": int(r["tokens"] or 0),
                "generations": int(r["generations"]),
                "seconds": round(float(r["seconds"] or 0), 2),
                "exact": bool(r["all_reported"]),
            }
            for r in rows
        ]

    def _grouped(self, column: str, period: Period) -> list[dict[str, Any]]:
        start, _ = period_bounds(period)
        where, params = ("WHERE ts >= ?", [start]) if start else ("", [])
        rows = self.db.query(
            f"SELECT {column} AS key, SUM(prompt_tokens) AS prompt,"
            " SUM(completion_tokens) AS completion, SUM(total_tokens) AS total,"
            " COUNT(*) AS generations, SUM(total_duration_ns)/1e9 AS seconds,"
            " SUM(tool_calls) AS tools, MIN(token_source = 'reported') AS all_reported"
            f" FROM usage_records {where} GROUP BY {column} ORDER BY total DESC",
            params,
        )
        return [
            {
                "key": r["key"] or "(none)",
                "prompt_tokens": int(r["prompt"] or 0),
                "completion_tokens": int(r["completion"] or 0),
                "total_tokens": int(r["total"] or 0),
                "generations": int(r["generations"]),
                "tool_calls": int(r["tools"] or 0),
                "seconds": round(float(r["seconds"] or 0), 2),
                "tokens_per_second": (
                    round(int(r["completion"] or 0) / float(r["seconds"]), 2)
                    if float(r["seconds"] or 0) > 0
                    else None
                ),
                "exact": bool(r["all_reported"]),
            }
            for r in rows
        ]

    def daily_series(self, days: int = 14) -> list[dict[str, Any]]:
        """Per-day totals for the sparkline in the usage view."""
        start = (
            datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=days - 1)
        ).timestamp()
        rows = self.db.query(
            "SELECT date(ts, 'unixepoch', 'localtime') AS day,"
            " SUM(total_tokens) AS tokens, COUNT(*) AS generations"
            " FROM usage_records WHERE ts >= ? GROUP BY day ORDER BY day",
            (start,),
        )
        buckets = {r["day"]: (int(r["tokens"] or 0), int(r["generations"])) for r in rows}
        out: list[dict[str, Any]] = []
        for offset in range(days):
            day = (datetime.fromtimestamp(start) + timedelta(days=offset)).strftime("%Y-%m-%d")
            tokens, generations = buckets.get(day, (0, 0))
            out.append({"day": day, "total_tokens": tokens, "generations": generations})
        return out

    def report(
        self, period: Period = Period.ALL, *, assumed_watts: float | None = None
    ) -> dict[str, Any]:
        """The full report rendered by ``localai usage report --json``."""
        totals = self.totals(period)
        return {
            "schema": "localai.usage-report/1",
            "period": period.value,
            "generated_at": time.time(),
            "totals": totals.to_dict(),
            "by_model": self.by_model(period),
            "by_workspace": self.by_workspace(period),
            "by_conversation": self.by_conversation(period),
            "daily": self.daily_series(),
            "notes": [
                "prompt_tokens and completion_tokens are exact when token_exactness is 'exact'; "
                "Ollama reported them directly.",
                "thinking_tokens are always an estimate: Ollama counts reasoning inside "
                "completion tokens and does not report it separately.",
                "energy_wh_estimate is a rough calculation from assumed system draw "
                f"({assumed_watts or 'not configured'} W) multiplied by generation wall time. "
                "It is not a measurement.",
            ],
        }
