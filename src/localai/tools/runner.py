"""The single execution path for every tool call.

This module is the chokepoint. Whatever the caller -- agent loop, CLI ``test-tool``,
local API, MCP server -- a tool runs only by passing through :meth:`ToolRunner.execute`,
which in fixed order:

1. resolves the tool and validates arguments against its schema;
2. builds a :class:`PermissionRequest` from the tool's declared risk and paths;
3. asks the permissions engine for a decision;
4. obtains user confirmation when required, via an injected callback;
5. executes under a timeout, honouring cancellation;
6. truncates output, spilling the full text to disk;
7. scans untrusted output for prompt injection and wraps it;
8. writes audit records for both the decision and the outcome.

Steps 3, 4 and 8 cannot be skipped by a caller: there is no parameter that disables
them. Adding one would be a security-relevant change requiring the tests in
``tests/unit/test_permission_boundaries.py`` to be revisited.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from localai.errors import LocalAIError, PermissionDeniedError, ToolValidationError
from localai.permissions.audit import AuditLogger
from localai.permissions.engine import (
    Caller,
    Decision,
    Effect,
    PermissionEngine,
    PermissionRequest,
)
from localai.safety import injection
from localai.tools.base import Tool, ToolContext, ToolResult
from localai.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

#: Called when a decision requires confirmation. Returns True to proceed.
#: The signature carries the full decision so a UI can explain *why* it is asking.
ConfirmCallback = Callable[[Tool, dict[str, Any], Decision], Awaitable[bool]]


async def deny_all(tool: Tool, arguments: dict[str, Any], decision: Decision) -> bool:
    """Default confirmation handler: refuse.

    Non-interactive callers get a safe default. A CLI or API that cannot prompt must
    not silently behave as though the user said yes.
    """
    return False


class ToolRunner:
    """Executes tools under policy."""

    def __init__(
        self,
        registry: ToolRegistry,
        engine: PermissionEngine,
        audit: AuditLogger,
        *,
        confirm: ConfirmCallback | None = None,
    ) -> None:
        self.registry = registry
        self.engine = engine
        self.audit = audit
        self.confirm = confirm or deny_all

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
        *,
        caller: Caller | None = None,
        call_id: str = "",
    ) -> ToolResult:
        """Run one tool call end to end. Returns a result even on failure.

        Failures are returned rather than raised because the result goes back to the
        model, which can often recover -- by correcting an argument, or choosing a
        different approach. Only cancellation propagates.
        """
        started = time.perf_counter()
        caller = caller or Caller()

        # 1. Resolve and validate ------------------------------------------------
        try:
            tool = self.registry.get(name)
        except LocalAIError as exc:
            # The remediation carries the "did you mean ...?" suggestion. It goes into
            # the model's context, so folding it into the message is what lets the
            # model correct itself on the next iteration instead of repeating the
            # same bad call.
            detail = f"{exc.message} {exc.remediation}".strip()
            return ToolResult.failure(detail, call_id=call_id)
        except Exception as exc:
            return ToolResult.failure(str(exc), call_id=call_id)

        try:
            validated = self.registry.validate_arguments(tool, arguments)
        except ToolValidationError as exc:
            self.audit.record_event("tool_validation_failed", caller, tool=name, reason=exc.message)
            return ToolResult.failure(exc.message, call_id=call_id, tool=name)

        # 2. Build the permission request ---------------------------------------
        try:
            paths = tool.affected_paths(validated)
        except Exception:
            log.exception("%s.affected_paths raised; treating as no declared paths", name)
            paths = []

        request = PermissionRequest(
            tool=tool.name,
            risk=tool.risk,
            mutating=tool.mutating,
            network=tool.network,
            paths=paths,
            command=validated.get("command") if isinstance(validated.get("command"), str) else None,
            arguments=validated,
            caller=caller,
            cwd=context.cwd,
        )

        # 3. Decide ---------------------------------------------------------------
        decision = self.engine.evaluate(request)
        self.audit.record_decision(
            request, decision, outcome="pending", conversation_id=context.conversation_id
        )

        if decision.effect is Effect.DENY:
            self.audit.record_decision(
                request, decision, outcome="denied", conversation_id=context.conversation_id
            )
            return ToolResult.failure(
                f"Permission denied: {decision.reason}",
                call_id=call_id,
                tool=name,
                stage=decision.stage,
                overridable=decision.overridable,
            )

        # 4. Confirm --------------------------------------------------------------
        confirmed: bool | None = None
        if decision.effect is Effect.CONFIRM:
            confirmed = await self.confirm(tool, validated, decision)
            self.audit.record_decision(
                request,
                decision,
                outcome="executed" if confirmed else "cancelled",
                conversation_id=context.conversation_id,
                confirmed_by_user=confirmed,
            )
            if not confirmed:
                return ToolResult.failure(
                    "The user declined this action. Do not retry it; ask what they would "
                    "prefer instead.",
                    call_id=call_id,
                    tool=name,
                    declined=True,
                )

        # 5. Execute ---------------------------------------------------------------
        timeout = self._timeout_for(tool, context)
        try:
            result = await asyncio.wait_for(tool.run(validated, context), timeout=timeout)
        except TimeoutError:
            elapsed = (time.perf_counter() - started) * 1000
            self.audit.record_execution(
                request,
                decision,
                outcome="failed",
                duration_ms=elapsed,
                error=f"timeout after {timeout}s",
                conversation_id=context.conversation_id,
            )
            return ToolResult.failure(
                f"{name} exceeded its {timeout:.0f}s timeout and was stopped. "
                "Narrow the request, or raise safety.tool_timeout_s.",
                call_id=call_id,
                tool=name,
                timeout_s=timeout,
            )
        except asyncio.CancelledError:
            self.audit.record_execution(
                request,
                decision,
                outcome="cancelled",
                duration_ms=(time.perf_counter() - started) * 1000,
                conversation_id=context.conversation_id,
            )
            raise
        except Exception as exc:  # a tool bug must not take the session down
            log.exception("tool %s raised", name)
            self.audit.record_execution(
                request,
                decision,
                outcome="failed",
                duration_ms=(time.perf_counter() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}",
                conversation_id=context.conversation_id,
            )
            return ToolResult.failure(f"{type(exc).__name__}: {exc}", call_id=call_id, tool=name)

        # 6-7. Post-process ---------------------------------------------------------
        result = self._truncate(result, tool, context)
        result = self._screen_untrusted(result, tool, context)
        if confirmed is not None:
            result.metadata["confirmed_by_user"] = confirmed
        result.metadata.setdefault("tool", name)
        result.metadata.setdefault("call_id", call_id)

        # 8. Audit the outcome --------------------------------------------------------
        self.audit.record_execution(
            request,
            decision,
            outcome="dry_run"
            if context.dry_run and tool.mutating
            else ("executed" if result.ok else "failed"),
            duration_ms=(time.perf_counter() - started) * 1000,
            error=result.error,
            injection_signals=[f for f in result.flags if f.startswith("injection:")],
            conversation_id=context.conversation_id,
        )
        return result

    async def preview(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
        *,
        caller: Caller | None = None,
    ) -> tuple[Decision, str]:
        """Evaluate without executing. Powers ``localai permissions explain``.

        Sharing the request-construction path with :meth:`execute` is deliberate:
        an explanation that used different logic from enforcement would be worse
        than no explanation.
        """
        tool = self.registry.get(name)
        validated = self.registry.validate_arguments(tool, arguments)
        request = PermissionRequest(
            tool=tool.name,
            risk=tool.risk,
            mutating=tool.mutating,
            network=tool.network,
            paths=tool.affected_paths(validated),
            command=validated.get("command") if isinstance(validated.get("command"), str) else None,
            arguments=validated,
            caller=caller or Caller(),
            cwd=context.cwd,
        )
        return self.engine.evaluate(request), tool.describe_call(validated)

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _timeout_for(tool: Tool, context: ToolContext) -> float:
        safety = context.config.safety
        return safety.shell_timeout_s if tool.category == "shell" else safety.tool_timeout_s

    def _truncate(self, result: ToolResult, tool: Tool, context: ToolContext) -> ToolResult:
        """Cap content size, writing the full text to disk first.

        Truncating without preserving the original would lose evidence the user may
        need, so the full output is always spilled and its path reported both to the
        model and in the UI.
        """
        limit = context.config.safety.max_output_bytes
        encoded = result.content.encode("utf-8", "replace")
        if len(encoded) <= limit:
            return result

        try:
            context.paths.tool_output_dir.mkdir(parents=True, exist_ok=True)
            spill = context.paths.tool_output_dir / f"{tool.name}-{int(time.time() * 1000)}.txt"
            spill.write_text(result.content, encoding="utf-8", errors="replace")
            result.full_output_path = spill
        except OSError:
            log.exception("could not spill full tool output")
            spill = None

        head = encoded[: int(limit * 0.7)].decode("utf-8", "ignore")
        tail = encoded[-int(limit * 0.2) :].decode("utf-8", "ignore")
        location = f"\nFull output: {spill}" if spill else "\n(full output could not be saved)"
        result.content = (
            f"{head}\n\n... [truncated: {len(encoded):,} bytes total, "
            f"{len(encoded) - len(head) - len(tail):,} omitted]{location}\n\n{tail}"
        )
        result.truncated = True
        result.flags.append("truncated")
        result.metadata["original_bytes"] = len(encoded)
        return result

    def _screen_untrusted(self, result: ToolResult, tool: Tool, context: ToolContext) -> ToolResult:
        """Scan and fence content that came from files or command output."""
        if not (tool.returns_untrusted_content or result.untrusted) or not result.content:
            return result

        findings = (
            injection.scan(result.content) if context.config.safety.detect_prompt_injection else []
        )
        if findings:
            result.flags.extend(f"injection:{f.signal.value}" for f in findings)
            result.metadata["injection_findings"] = [f.to_dict() for f in findings]
            log.warning(
                "possible prompt injection in %s output: %s",
                tool.name,
                "; ".join(f.reason for f in findings),
            )
        result.content = injection.wrap_untrusted(
            result.content, source=tool.name, findings=findings
        )
        return result


def path_from(arguments: dict[str, Any], *keys: str) -> list[Path]:
    """Collect path-valued arguments. Used by tools' ``affected_paths``."""
    out: list[Path] = []
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            out.append(Path(value))
        elif isinstance(value, list):
            out.extend(Path(v) for v in value if isinstance(v, str) and v.strip())
    return out


__all__ = [
    "ConfirmCallback",
    "PermissionDeniedError",
    "ToolRunner",
    "deny_all",
    "path_from",
]
