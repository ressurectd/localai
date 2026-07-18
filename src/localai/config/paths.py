"""Resolution of every on-disk location the application uses.

All path decisions live here so that tests, portable mode and the `LOCALAI_HOME`
override each have exactly one place to intervene. Nothing else in the codebase may
call ``os.environ`` for a directory or hard-code ``~/.localai``.

Layout under the resolved home directory::

    config.toml          user configuration (atomically written)
    profiles/            saved model profiles (one TOML per profile)
    data/localai.db      conversations, usage, audit log, permission rules
    data/index/          document indexes (Phase 3)
    logs/audit.jsonl     append-only audit mirror
    logs/localai.log     structured application log
    backups/             pre-modification file backups
    plugins/             third-party tool plugins
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Set to any value to run in portable mode: state is kept next to the executable.
ENV_PORTABLE = "LOCALAI_PORTABLE"
#: Absolute path override for the whole state directory. Used by tests and sandboxes.
ENV_HOME = "LOCALAI_HOME"


def default_home() -> Path:
    """Return the state directory, honouring ``LOCALAI_HOME`` then portable mode.

    On Windows the default is ``%LOCALAPPDATA%\\localai``; elsewhere it is
    ``$XDG_DATA_HOME/localai`` falling back to ``~/.local/share/localai``. Portable
    mode places state in a ``localai-data`` folder beside the installed package,
    which keeps a USB-stick install fully self-contained.
    """
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser()

    if os.environ.get(ENV_PORTABLE):
        return Path(__file__).resolve().parents[3] / "localai-data"

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "localai"

    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) / "localai" if xdg else Path.home() / ".local" / "share" / "localai"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Every directory and file the application owns, derived from one root."""

    home: Path

    @classmethod
    def resolve(cls, home: Path | None = None) -> AppPaths:
        return cls(home=(home or default_home()).expanduser())

    # --- files ---------------------------------------------------------------
    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"

    @property
    def database(self) -> Path:
        return self.data_dir / "localai.db"

    @property
    def audit_log(self) -> Path:
        return self.log_dir / "audit.jsonl"

    @property
    def app_log(self) -> Path:
        return self.log_dir / "localai.log"

    # --- directories ---------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def profile_dir(self) -> Path:
        return self.home / "profiles"

    @property
    def backup_dir(self) -> Path:
        return self.home / "backups"

    @property
    def plugin_dir(self) -> Path:
        return self.home / "plugins"

    @property
    def tool_output_dir(self) -> Path:
        """Full, untruncated tool output is spilled here and referenced by path."""
        return self.data_dir / "tool-output"

    def ensure(self) -> AppPaths:
        """Create every directory. Safe to call repeatedly; returns self for chaining."""
        for directory in (
            self.home,
            self.data_dir,
            self.index_dir,
            self.log_dir,
            self.profile_dir,
            self.backup_dir,
            self.plugin_dir,
            self.tool_output_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def protected_paths(self) -> tuple[Path, ...]:
        """Paths that tools may never modify, regardless of permission mode.

        This is the concrete expression of two invariants from the security model:
        the model cannot silently rewrite the permission database, and it cannot
        disable the audit log. ``PermissionEngine`` denies mutation of anything at
        or beneath these paths *before* consulting any rule, mode or grant, so no
        configuration can weaken it.
        """
        return (self.config_file, self.database, self.audit_log, self.app_log, self.profile_dir)
