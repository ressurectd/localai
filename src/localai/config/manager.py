"""Loading, validating, overriding and atomically persisting configuration.

Precedence, lowest to highest: built-in defaults, ``config.toml``, environment
variables, explicit runtime overrides (CLI flags). Every environment variable this
project reads is declared in :data:`ENV_OVERRIDES` -- there are no undocumented ones.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Final

import tomli_w
from pydantic import ValidationError

from localai.config.models import Config
from localai.config.paths import AppPaths
from localai.errors import ConfigError

#: Environment variable -> dotted config path. This mapping *is* the documentation;
#: `localai config schema --env` prints it, and docs/development.md links to it.
ENV_OVERRIDES: Final[dict[str, str]] = {
    "LOCALAI_MODEL": "default_model",
    "LOCALAI_PROFILE": "default_profile",
    "LOCALAI_OLLAMA_HOST": "ollama.host",
    "LOCALAI_OLLAMA_PORT": "ollama.port",
    "LOCALAI_PERMISSION_MODE": "permissions.mode",
    "LOCALAI_KILL_SWITCH": "permissions.kill_switch",
    "LOCALAI_READ_ONLY": "safety.read_only",
    "LOCALAI_DRY_RUN": "safety.dry_run",
    "LOCALAI_NETWORK_DISABLED": "privacy.network_disabled",
    "LOCALAI_THEME": "ui.theme",
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _coerce(raw: str) -> Any:
    """Convert an environment string to bool/int/float where unambiguous."""
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _assign(target: dict[str, Any], dotted: str, value: Any) -> None:
    """Set ``a.b.c`` inside a nested dict, creating intermediate dicts as needed."""
    *parents, leaf = dotted.split(".")
    cursor = target
    for part in parents:
        nxt = cursor.setdefault(part, {})
        if not isinstance(nxt, dict):  # a scalar already occupies this slot
            raise ConfigError(
                f"cannot apply override {dotted!r}: {part!r} is not a section",
                remediation="Check config.toml for a key that collides with a section name.",
            )
        cursor = nxt
    cursor[leaf] = value


def apply_env_overrides(raw: dict[str, Any], env: dict[str, str] | None = None) -> list[str]:
    """Apply :data:`ENV_OVERRIDES` in place; return the names of the vars that applied.

    Returned names are surfaced by ``localai doctor`` so a user can see at a glance
    why their configuration differs from the file on disk.
    """
    source = os.environ if env is None else env
    applied: list[str] = []
    for var, dotted in ENV_OVERRIDES.items():
        if (value := source.get(var)) not in (None, ""):
            _assign(raw, dotted, _coerce(value))
            applied.append(var)
    return applied


def _format_validation_error(exc: ValidationError, origin: str) -> str:
    """Render pydantic errors as one actionable line per problem."""
    lines = [f"{len(exc.errors())} problem(s) in {origin}:"]
    for err in exc.errors():
        location = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  - {location}: {err['msg']}")
    return "\n".join(lines)


class ConfigManager:
    """Owns the lifecycle of the :class:`Config` object for one process.

    Held by the application container and injected everywhere else; nothing reads
    configuration from a module-level global.
    """

    def __init__(self, paths: AppPaths, *, env: dict[str, str] | None = None) -> None:
        self.paths = paths
        self._env = env
        self._config: Config | None = None
        self.applied_env_vars: list[str] = []

    @property
    def config(self) -> Config:
        """The validated configuration, loaded on first access."""
        if self._config is None:
            self._config = self.load()
        return self._config

    def load(self) -> Config:
        """Read, override and validate. Raises :class:`ConfigError` with detail."""
        raw: dict[str, Any] = {}
        if self.paths.config_file.exists():
            try:
                raw = tomllib.loads(self.paths.config_file.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(
                    f"{self.paths.config_file} is not valid TOML: {exc}",
                    remediation="Fix the syntax error, or delete the file to regenerate defaults.",
                    path=str(self.paths.config_file),
                ) from exc
            except OSError as exc:
                raise ConfigError(
                    f"cannot read {self.paths.config_file}: {exc}",
                    remediation="Check file permissions on the localai home directory.",
                ) from exc

        self.applied_env_vars = apply_env_overrides(raw, self._env)

        try:
            self._config = Config.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(
                _format_validation_error(exc, str(self.paths.config_file)),
                remediation="Run 'localai config schema' to see the expected structure.",
                path=str(self.paths.config_file),
            ) from exc
        return self._config

    def save(self, config: Config | None = None) -> Path:
        """Persist configuration atomically.

        Writes to a temporary file in the same directory, flushes to the platform's
        disk cache, then performs an atomic replace. A crash mid-write therefore
        leaves the previous configuration intact rather than a truncated file --
        this is the "atomic writes for important configuration" requirement.
        """
        target = config or self.config
        self.paths.ensure()
        payload = target.model_dump(mode="json", exclude_none=True, exclude_defaults=False)

        handle = tempfile.NamedTemporaryFile(
            mode="wb", dir=self.paths.home, prefix=".config-", suffix=".toml", delete=False
        )
        tmp = Path(handle.name)
        try:
            with handle:
                handle.write(tomli_w.dumps(_toml_safe(payload)).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            # os.replace, not Path.replace: this is the atomic-rename primitive and
            # the call is the whole point of the function.
            os.replace(tmp, self.paths.config_file)  # noqa: PTH105
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._config = target
        return self.paths.config_file

    def init_if_missing(self) -> bool:
        """Write a default configuration if none exists. Returns True if created."""
        if self.paths.config_file.exists():
            return False
        self.save(Config())
        return True


def _toml_safe(value: Any) -> Any:
    """Strip ``None`` values, which TOML cannot represent, and stringify Paths."""
    if isinstance(value, dict):
        return {k: _toml_safe(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_toml_safe(v) for v in value if v is not None]
    if isinstance(value, Path):
        return str(value)
    return value
