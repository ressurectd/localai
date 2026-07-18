"""Structured tool-calling for models without native support.

``mock-plain:3b``, older Llama builds and many fine-tunes have no ``tools``
capability. Rather than denying them tool use, we ask for a fenced JSON block and
parse it out of the response text.

This is genuinely less reliable than native tool calling and the application says so
rather than pretending otherwise: the UI marks fallback turns, and
:func:`build_instructions` is written to maximise the chance that a small model
complies -- one example, an explicit "exactly one block", and a stated stop rule.

The parser is deliberately forgiving about *framing* (fence style, whitespace,
a stray prose preamble) and strict about *content* (the JSON must have a ``tool``
string and an ``arguments`` object). Forgiving framing costs nothing; forgiving
content would mean executing a call the model did not clearly specify.
"""

from __future__ import annotations

import json
import re

from localai.domain.messages import ToolCall
from localai.tools.base import Tool

#: Matches ```tool_call ... ``` or ```json ... ``` or a bare ``` ... ``` fence.
_FENCE = re.compile(
    r"```(?:tool_call|tool|json)?\s*\n(?P<body>.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)

#: Fallback for models that emit a bare JSON object with no fence at all.
_BARE_OBJECT = re.compile(r'(?P<body>\{\s*"tool"\s*:.*?\})\s*$', re.DOTALL)


def build_instructions(tools: list[Tool]) -> str:
    """Compose the system-prompt section describing the fallback protocol."""
    if not tools:
        return ""

    catalogue = "\n".join(
        f"- {tool.name}: {tool.description}\n"
        f"  arguments: {json.dumps(tool.parameters.get('properties', {}), separators=(',', ':'))}\n"
        f"  required: {json.dumps(tool.parameters.get('required', []))}"
        for tool in tools
    )
    return f"""
## Using tools

You can run tools on the user's computer. To call one, reply with a single fenced
block and nothing else:

```tool_call
{{"tool": "read_file", "arguments": {{"path": "C:/notes/todo.txt"}}}}
```

Rules:
1. Emit exactly ONE tool_call block per reply. Do not emit two.
2. The block must contain only valid JSON with a "tool" string and an "arguments" object.
3. Do not add explanation before or after the block when calling a tool.
4. After the tool result is returned to you, either call another tool or give your
   final answer as plain text with no block.
5. When you have enough information, answer in plain text. Do not call a tool again
   just to confirm something you already know.

Available tools:
{catalogue}
""".strip()


def parse_tool_calls(text: str) -> tuple[list[ToolCall], str]:
    """Extract tool calls from response text.

    Returns ``(calls, remaining_text)`` where ``remaining_text`` has the parsed
    blocks removed, so any genuine prose the model wrote is still shown to the user
    rather than silently swallowed.
    """
    calls: list[ToolCall] = []
    spans: list[tuple[int, int]] = []

    for match in _FENCE.finditer(text):
        if (call := _parse_object(match.group("body"))) is not None:
            calls.append(call)
            spans.append(match.span())

    if not calls and (bare := _BARE_OBJECT.search(text.strip())):
        if (call := _parse_object(bare.group("body"))) is not None:
            calls.append(call)
            spans.append(bare.span())

    remaining = text
    for start, end in reversed(spans):
        remaining = remaining[:start] + remaining[end:]
    return calls, remaining.strip()


def _parse_object(body: str) -> ToolCall | None:
    """Parse one candidate JSON object into a ToolCall, or None if it is not one."""
    body = body.strip()
    if not body.startswith("{"):
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        # Small models often emit trailing commas or single quotes. One repair pass
        # is worth it; anything beyond that risks executing a call we guessed at.
        repaired = re.sub(r",\s*([}\]])", r"\1", body)
        try:
            data = json.loads(repaired)
        except json.JSONDecodeError:
            return None

    if not isinstance(data, dict):
        return None

    # Accept the common aliases models produce for the same two fields.
    name = data.get("tool") or data.get("name") or data.get("function")
    arguments = data.get("arguments")
    if arguments is None:
        arguments = data.get("args") or data.get("parameters") or {}

    if not isinstance(name, str) or not name.strip():
        return None
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None

    return ToolCall(name=name.strip(), arguments=arguments)


def format_tool_result(name: str, content: str) -> str:
    """Render a tool result for a model that has no ``tool`` role.

    Fallback models receive results as a user-role message, so the framing has to
    make it unambiguous that this is machine output rather than the user speaking.
    """
    return f"[tool result: {name}]\n{content}\n[end tool result]\n\nContinue, or give your final answer."


def has_native_tools(capabilities: frozenset[str] | set[str]) -> bool:
    return "tools" in capabilities


__all__: list[str] = [
    "build_instructions",
    "format_tool_result",
    "has_native_tools",
    "parse_tool_calls",
]
