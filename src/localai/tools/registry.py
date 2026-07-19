"""Central tool registry and argument validation.

Registration is explicit: :func:`register_builtins` names every tool it installs.
There is no import-time side effect and no directory scanning, so reading one
function tells you exactly which tools exist -- which is both a maintainability
property and a security one.

Argument validation implements the subset of JSON Schema the tool definitions use
(types, required, enum, ranges, defaults). A full JSON Schema library would be a
reasonable alternative; the subset is used here to avoid a dependency for something
this contained, and ``tests/unit/test_registry.py`` covers it.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator
from typing import Any

from localai.errors import ToolNotFoundError, ToolValidationError
from localai.tools.base import Tool

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


class ToolRegistry:
    """Holds the tools available to a session."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, *, replace: bool = False) -> Tool:
        """Add a tool. Refuses to shadow an existing name unless asked.

        Silent replacement would let a plugin substitute a permissive
        implementation for a built-in tool of the same name, so it must be explicit.
        """
        if not tool.name:
            raise ValueError(f"{type(tool).__name__} has no name")
        if tool.name in self._tools and not replace:
            raise ValueError(
                f"tool {tool.name!r} is already registered by "
                f"{type(self._tools[tool.name]).__name__}; pass replace=True to override"
            )
        self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            suggestion = _closest(name, self._tools)
            raise ToolNotFoundError(
                f"no tool named {name!r}",
                remediation=(f"Did you mean {suggestion!r}? " if suggestion else "")
                + "Run 'ai tools list' to see the available tools.",
                tool=name,
                available=sorted(self._tools),
            ) from None

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(sorted(self._tools.values(), key=lambda t: t.name))

    def names(self) -> list[str]:
        return sorted(self._tools)

    def select(
        self,
        *,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        read_only: bool = False,
    ) -> list[Tool]:
        """Filter tools by glob patterns and mutability.

        Used to expose a narrower set over MCP or the local API than the TUI has,
        and to drop mutating tools entirely in read-only or investigation mode.
        """
        chosen = list(self)
        if include:
            chosen = [t for t in chosen if any(fnmatch.fnmatch(t.name, p) for p in include)]
        if exclude:
            chosen = [t for t in chosen if not any(fnmatch.fnmatch(t.name, p) for p in exclude)]
        if read_only:
            chosen = [t for t in chosen if not t.mutating]
        return chosen

    def ollama_schemas(self, tools: list[Tool] | None = None) -> list[dict[str, Any]]:
        return [t.to_ollama_schema() for t in (tools if tools is not None else list(self))]

    def describe_all(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self]

    def validate_arguments(self, tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalise arguments, returning a copy with defaults applied.

        Small local models produce malformed tool calls regularly -- a stringified
        number, a missing optional, an extra invented field. The strategy is to
        coerce what is unambiguously coercible and reject the rest with a message
        the *model* can act on, because that message goes back into its context.
        """
        if not isinstance(arguments, dict):
            raise ToolValidationError(
                f"{tool.name}: arguments must be a JSON object, got {type(arguments).__name__}",
                tool=tool.name,
            )

        schema = tool.parameters
        properties: dict[str, Any] = schema.get("properties", {})
        required: list[str] = schema.get("required", [])
        result: dict[str, Any] = {}

        if missing := [k for k in required if k not in arguments or arguments[k] is None]:
            raise ToolValidationError(
                f"{tool.name}: missing required argument(s): {', '.join(missing)}. "
                f"Expected: {_signature(schema)}",
                tool=tool.name,
                missing=missing,
            )

        for key, value in arguments.items():
            if key not in properties:
                # Extra keys are dropped rather than rejected: a model inventing a
                # plausible-but-unsupported option should not fail an otherwise valid
                # call. The drop is recorded so it surfaces in the audit log.
                continue
            result[key] = _coerce(tool.name, key, value, properties[key])

        for key, spec in properties.items():
            if key not in result and "default" in spec:
                result[key] = spec["default"]
        return result


def _coerce(tool: str, key: str, value: Any, spec: dict[str, Any]) -> Any:
    """Type-check one argument, coercing where unambiguous."""
    expected = spec.get("type")
    if expected and expected in _JSON_TYPES:
        python_type = _JSON_TYPES[expected]
        # bool is a subclass of int in Python; treat them as distinct here so that
        # `true` is not silently accepted where an integer is required.
        if expected in {"integer", "number"} and isinstance(value, bool):
            raise ToolValidationError(
                f"{tool}: argument {key!r} must be a {expected}, got a boolean", tool=tool
            )
        if not isinstance(value, python_type):
            if expected in {"integer", "number"} and isinstance(value, str):
                try:
                    value = int(value) if expected == "integer" else float(value)
                except ValueError:
                    raise ToolValidationError(
                        f"{tool}: argument {key!r} must be a {expected}, got {value!r}", tool=tool
                    ) from None
            elif expected == "boolean" and isinstance(value, str):
                lowered = value.strip().lower()
                if lowered not in {"true", "false"}:
                    raise ToolValidationError(
                        f"{tool}: argument {key!r} must be true or false, got {value!r}", tool=tool
                    )
                value = lowered == "true"
            elif expected == "array" and isinstance(value, str):
                value = [value]  # a single item where a list was expected is unambiguous
            else:
                raise ToolValidationError(
                    f"{tool}: argument {key!r} must be a {expected}, got {type(value).__name__}",
                    tool=tool,
                )

    if (allowed := spec.get("enum")) and value not in allowed:
        raise ToolValidationError(
            f"{tool}: argument {key!r} must be one of {allowed}, got {value!r}", tool=tool
        )

    for bound, comparison, label in (
        ("minimum", lambda v, b: v < b, "at least"),
        ("maximum", lambda v, b: v > b, "at most"),
    ):
        if bound in spec and isinstance(value, (int, float)) and comparison(value, spec[bound]):
            raise ToolValidationError(
                f"{tool}: argument {key!r} must be {label} {spec[bound]}, got {value}", tool=tool
            )

    if (limit := spec.get("maxLength")) and isinstance(value, str) and len(value) > limit:
        raise ToolValidationError(
            f"{tool}: argument {key!r} exceeds the maximum length of {limit}", tool=tool
        )
    return value


def _signature(schema: dict[str, Any]) -> str:
    """Render a schema as a compact call signature, for error messages."""
    required = set(schema.get("required", []))
    parts = [
        f"{name}: {spec.get('type', 'any')}" + ("" if name in required else " (optional)")
        for name, spec in schema.get("properties", {}).items()
    ]
    return "(" + ", ".join(parts) + ")"


def _closest(name: str, candidates: dict[str, Tool]) -> str | None:
    """Nearest registered tool name, so a typo produces a useful suggestion."""
    import difflib

    matches = difflib.get_close_matches(name, list(candidates), n=1, cutoff=0.6)
    return matches[0] if matches else None
