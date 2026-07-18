"""Exception hierarchy.

Every exception carries a stable ``code`` and a ``remediation`` string. The CLI turns
these into structured stderr output and a meaningful exit status, so an external agent
can react to a failure without parsing prose. Adding a new error type means adding a
new code here rather than raising a bare ``RuntimeError`` from a module.
"""

from __future__ import annotations

from typing import Any


class LocalAIError(Exception):
    """Base class for all errors this application raises deliberately."""

    code = "localai_error"
    exit_code = 1

    def __init__(self, message: str, *, remediation: str = "", **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        """Serialise for ``--json`` error envelopes."""
        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
            "details": self.details,
        }


class ConfigError(LocalAIError):
    """Configuration file is missing, malformed or fails validation."""

    code = "config_error"
    exit_code = 78  # EX_CONFIG


class ProviderError(LocalAIError):
    """The model provider (Ollama) is unreachable or returned an error."""

    code = "provider_error"
    exit_code = 69  # EX_UNAVAILABLE


class ProviderUnavailableError(ProviderError):
    """The Ollama daemon could not be contacted at all."""

    code = "provider_unavailable"


class ModelNotFoundError(ProviderError):
    """The requested model is not installed locally."""

    code = "model_not_found"
    exit_code = 66  # EX_NOINPUT


class ToolError(LocalAIError):
    """A tool failed while executing. Recoverable: reported back to the model."""

    code = "tool_error"


class ToolNotFoundError(ToolError):
    """No tool is registered under the requested name."""

    code = "tool_not_found"
    exit_code = 66


class ToolValidationError(ToolError):
    """Tool arguments did not satisfy the tool's JSON Schema."""

    code = "tool_validation_error"


class ToolTimeoutError(ToolError):
    """A tool exceeded its configured timeout and was terminated."""

    code = "tool_timeout"


class PermissionDeniedError(LocalAIError):
    """The permissions engine refused an action. Never retried automatically."""

    code = "permission_denied"
    exit_code = 77  # EX_NOPERM


class PathSafetyError(PermissionDeniedError):
    """A path escaped its workspace, or resolved through a symlink/junction."""

    code = "path_safety_error"


class StorageError(LocalAIError):
    """The SQLite database is unavailable, corrupt or a migration failed."""

    code = "storage_error"
    exit_code = 74  # EX_IOERR


class MigrationError(StorageError):
    """A schema migration could not be applied."""

    code = "migration_error"


class CancelledError(LocalAIError):
    """The user cancelled an in-flight operation. Not a failure."""

    code = "cancelled"
    exit_code = 130  # conventional for SIGINT


class AgentLoopError(LocalAIError):
    """The agent loop hit a guard rail (iteration cap, repetition, wall clock)."""

    code = "agent_loop_error"
