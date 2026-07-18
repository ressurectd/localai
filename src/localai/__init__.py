"""localai - a local-first, agentic terminal UI for Ollama models.

The public entry points are :class:`localai.app.Application` (composition root) and
:func:`localai.cli.main.main` (CLI). Everything else is internal; see AGENTS.md.
"""

from localai.version import APP_VERSION as __version__  # noqa: N811 - PEP 396 dunder

__all__ = ["__version__"]
