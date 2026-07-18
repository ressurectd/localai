"""Example plugin: a word-count tool.

A plugin is a module exposing a ``build_tools()`` function returning ``list[Tool]``,
plus a ``plugin.json`` manifest declaring what those tools can do. The manifest is
what a user reviews *before* loading the plugin — which is why risk and mutability are
declared there rather than discovered at import time.

The loader ships in Phase 3. The manifest format and this interface are stable now.

To try the tool without the loader:

    localai test-tool word_count --args '{"path":"README.md"}' --json
    # (after registering it manually in a Python session)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from localai.config.models import RiskLevel
from localai.tools.base import Tool, ToolContext, ToolResult
from localai.tools.runner import path_from


class WordCount(Tool):
    """Count words, lines and characters in a text file."""

    name = "word_count"
    description = (
        "Count the words, lines and characters in a text file. Use this to judge the "
        "size of a document before deciding whether to read it in full."
    )
    category = "filesystem"
    risk = RiskLevel.READ
    mutating = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The text file to measure."},
        },
        "required": ["path"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        # Required for anything touching the filesystem. A path the permissions engine
        # never sees is a path it cannot contain.
        return path_from(arguments, "path")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        # Exactly what the user is asked to authorise.
        return f"count words in {arguments.get('path')}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = Path(str(arguments["path"]))
        if not path.is_absolute():
            path = context.cwd / path

        if not path.is_file():
            # An expected failure: return it so the model can read it and adapt,
            # rather than raising.
            return ToolResult.failure(
                f"{path} is not a file. Use list_directory to see what is there."
            )

        limit = context.config.safety.max_read_bytes

        def measure() -> tuple[int, int, int, bool]:
            """Stream the file so a huge one never lands in memory."""
            words = lines = characters = 0
            truncated = False
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if characters >= limit:
                        truncated = True
                        break
                    lines += 1
                    words += len(line.split())
                    characters += len(line)
            return words, lines, characters, truncated

        # Blocking I/O goes to a thread so streaming and cancellation stay responsive.
        words, lines, characters, truncated = await asyncio.to_thread(measure)

        note = f"\n(stopped at the {limit:,}-byte read limit)" if truncated else ""
        return ToolResult(
            content=(
                f"{path.name}\n"
                f"  lines       {lines:,}\n"
                f"  words       {words:,}\n"
                f"  characters  {characters:,}{note}"
            ),
            metadata={
                "path": str(path),
                "lines": lines,
                "words": words,
                "characters": characters,
                "truncated": truncated,
            },
        )


def build_tools() -> list[Tool]:
    """Entry point named by ``plugin.json``. Returns the tools this plugin provides."""
    return [WordCount()]
