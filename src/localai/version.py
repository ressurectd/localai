"""Version constants for every externally visible interface.

Each public surface is versioned independently so that a change to one does not
force a bump of the others. External agents read these via ``localai version --json``
and should branch on the specific schema they consume, not on ``APP_VERSION``.
"""

from __future__ import annotations

from typing import Final

APP_VERSION: Final = "0.1.0"

#: Shape of ``--json`` output from the CLI. Additive changes keep the version;
#: removing or retyping a field requires a major bump plus a CHANGELOG entry.
CLI_SCHEMA_VERSION: Final = "1"

#: Version of the on-disk configuration format (``config.toml``).
CONFIG_SCHEMA_VERSION: Final = "1"

#: Version of the tool-definition / tool-result JSON contract.
TOOL_API_VERSION: Final = "1"

#: Version of the permission-rule format.
PERMISSION_SCHEMA_VERSION: Final = "1"

#: Version of the plugin manifest format.
PLUGIN_MANIFEST_VERSION: Final = "1"

#: Local HTTP API version (reserved; the API ships in Phase 4).
LOCAL_API_VERSION: Final = "v1"


def version_info() -> dict[str, str]:
    """Return every interface version as a flat mapping, for ``version --json``."""
    return {
        "app": APP_VERSION,
        "cli_schema": CLI_SCHEMA_VERSION,
        "config_schema": CONFIG_SCHEMA_VERSION,
        "tool_api": TOOL_API_VERSION,
        "permission_schema": PERMISSION_SCHEMA_VERSION,
        "plugin_manifest": PLUGIN_MANIFEST_VERSION,
        "local_api": LOCAL_API_VERSION,
    }
