"""Path resolution and containment checks.

Every path that reaches a tool passes through :func:`resolve_path`. Containment is
decided on the *fully resolved real path*, never on the string the model supplied,
because ``D:\\Trusted\\..\\..\\Windows`` and a junction named ``D:\\Trusted\\out``
pointing at ``C:\\`` both look harmless as text.

Windows specifics handled here:

* Junctions and directory symlinks are reparse points; ``Path.resolve()`` follows
  them, so comparing the resolved path against the resolved workspace root catches
  the escape. We additionally *report* which component was a reparse point so the
  user sees why an operation was blocked.
* Paths beyond ``MAX_PATH`` need the ``\\\\?\\`` prefix to be usable by the Win32
  API, which :func:`extended_path` applies.
* Drive-relative and UNC paths are normalised before comparison.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePath

WINDOWS = sys.platform == "win32"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MAX_PATH = 260

#: Reserved DOS device names. Opening these can hang or interact with hardware, and
#: a model that has read an attacker-authored filename should never reach them.
RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """The outcome of resolving one user- or model-supplied path."""

    original: str
    resolved: Path
    exists: bool
    is_dir: bool
    is_reparse_point: bool
    """True when the final component is a junction, symlink or other reparse point."""

    traversed_reparse_point: Path | None
    """The first ancestor that was a reparse point, if any. Explains an escape."""

    inside_workspace: Path | None
    """The trusted workspace containing this path, or None if outside all of them."""

    @property
    def escaped(self) -> bool:
        """True when a reparse point was traversed to reach a path outside its workspace."""
        return self.traversed_reparse_point is not None and self.inside_workspace is None


def is_reparse_point(path: Path) -> bool:
    """True if ``path`` itself is a junction, symlink or other reparse point.

    Uses ``lstat`` so we inspect the link, not its target. On Windows we check the
    reparse-point attribute directly, which catches junctions that ``is_symlink()``
    historically missed.
    """
    try:
        info = path.lstat()
    except (OSError, ValueError):
        return False
    if WINDOWS and getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    return stat.S_ISLNK(info.st_mode)


def extended_path(path: Path) -> str:
    """Return a Win32-safe string, applying the ``\\\\?\\`` prefix when needed.

    Only applied to absolute Windows paths longer than MAX_PATH: the prefix disables
    normalisation, so using it indiscriminately would change semantics for ordinary
    paths. UNC paths take the ``\\\\?\\UNC\\`` form.
    """
    text = str(path)
    if not WINDOWS or len(text) < MAX_PATH or text.startswith("\\\\?\\"):
        return text
    if not PurePath(text).is_absolute():
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def has_reserved_name(path: Path) -> str | None:
    """Return the offending component if any part is a reserved DOS device name."""
    if not WINDOWS:
        return None
    for part in path.parts:
        if part.split(".")[0].upper() in RESERVED_NAMES:
            return part
    return None


def _first_reparse_ancestor(path: Path) -> Path | None:
    """Walk from the root down, returning the first reparse point encountered.

    Reporting *which* component was a link turns an opaque denial into an
    explanation the user can act on.
    """
    current = Path(path.anchor) if path.anchor else Path(path.parts[0])
    for part in path.parts[1:]:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        if is_reparse_point(current):
            return current
    return None


def resolve_path(
    candidate: str | Path,
    *,
    workspaces: tuple[Path, ...] = (),
    base: Path | None = None,
) -> ResolvedPath:
    """Resolve ``candidate`` and determine which trusted workspace, if any, holds it.

    ``base`` is the current working directory used for relative paths. Resolution is
    non-strict so that a path being *created* (which does not yet exist) still gets
    a correct containment verdict from its resolved parent.
    """
    raw = str(candidate).strip().strip('"')
    path = Path(raw).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path

    try:
        resolved = path.resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        # Malformed paths (bad drive, cyclic link) resolve as far as they can; the
        # containment check below then fails closed because nothing will contain them.
        # abspath, not resolve(): resolve() already failed above, and abspath is
        # purely lexical so it cannot raise on the malformed input that got us here.
        resolved = Path(os.path.abspath(str(path)))  # noqa: PTH100

    containing: Path | None = None
    for workspace in workspaces:
        try:
            workspace_real = workspace.resolve(strict=False)
        except (OSError, ValueError):
            continue
        if resolved == workspace_real or resolved.is_relative_to(workspace_real):
            # Prefer the most specific workspace when several nest.
            if containing is None or len(str(workspace_real)) > len(str(containing)):
                containing = workspace_real

    exists = resolved.exists()
    return ResolvedPath(
        original=raw,
        resolved=resolved,
        exists=exists,
        is_dir=resolved.is_dir() if exists else False,
        is_reparse_point=is_reparse_point(path),
        traversed_reparse_point=_first_reparse_ancestor(path),
        inside_workspace=containing,
    )


def is_within(child: Path, parent: Path) -> bool:
    """True if ``child`` is at or beneath ``parent``, comparing resolved real paths."""
    try:
        c = child.resolve(strict=False)
        p = parent.resolve(strict=False)
    except (OSError, ValueError):
        return False
    return c == p or c.is_relative_to(p)
