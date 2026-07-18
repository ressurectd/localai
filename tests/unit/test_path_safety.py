"""Path traversal, junction escape and Windows path-handling tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from localai.config.models import Config
from localai.config.paths import AppPaths
from localai.permissions.engine import Effect, PermissionEngine
from localai.safety import pathsafe
from tests.unit.test_permission_boundaries import request_for

pytestmark = pytest.mark.security


def test_dotdot_traversal_resolves_outside_workspace(workspace: Path) -> None:
    """``..`` is resolved before containment is decided, not matched as a string."""
    resolved = pathsafe.resolve_path(
        workspace / "docs" / ".." / ".." / "escaped.txt", workspaces=(workspace,)
    )
    assert resolved.inside_workspace is None
    assert ".." not in str(resolved.resolved)


def test_nested_traversal_inside_workspace_stays_contained(workspace: Path) -> None:
    """A path that wanders but lands inside is still inside."""
    resolved = pathsafe.resolve_path(
        workspace / "docs" / ".." / "src" / "main.py", workspaces=(workspace,)
    )
    assert resolved.inside_workspace is not None
    assert resolved.resolved == (workspace / "src" / "main.py").resolve()


def test_absolute_path_outside_workspace_is_detected(workspace: Path, tmp_path: Path) -> None:
    resolved = pathsafe.resolve_path(tmp_path / "other" / "file.txt", workspaces=(workspace,))
    assert resolved.inside_workspace is None


def test_relative_path_resolves_against_base(workspace: Path) -> None:
    resolved = pathsafe.resolve_path("docs/notes.md", workspaces=(workspace,), base=workspace)
    assert resolved.inside_workspace is not None
    assert resolved.exists


def test_nonexistent_path_still_gets_a_containment_verdict(workspace: Path) -> None:
    """Creating a file needs a verdict before the file exists."""
    resolved = pathsafe.resolve_path(workspace / "new" / "file.txt", workspaces=(workspace,))
    assert resolved.exists is False
    assert resolved.inside_workspace is not None


def test_most_specific_workspace_wins(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    resolved = pathsafe.resolve_path(inner / "f.txt", workspaces=(outer, inner))
    assert resolved.inside_workspace == inner.resolve()


@pytest.mark.parametrize("name", ["CON", "NUL", "COM1", "LPT1", "con.txt", "nul.log"])
def test_reserved_device_names_are_detected(name: str, windows_only: None) -> None:
    assert pathsafe.has_reserved_name(Path(f"C:/temp/{name}")) is not None


def test_ordinary_names_are_not_flagged_as_reserved(windows_only: None) -> None:
    for name in ("console.txt", "communication.log", "printer.cfg", "content"):
        assert pathsafe.has_reserved_name(Path(f"C:/temp/{name}")) is None


def test_engine_denies_reserved_device_names(engine: PermissionEngine, windows_only: None) -> None:
    decision = engine.evaluate(request_for("read_file", paths=[Path("C:/temp/NUL")]))
    assert decision.effect is Effect.DENY
    assert decision.stage == "reserved_device_name"


def test_extended_path_prefix_applied_only_when_needed(windows_only: None) -> None:
    short = Path("C:/temp/file.txt")
    assert pathsafe.extended_path(short) == str(short)

    long = Path("C:/" + "/".join("d" * 30 for _ in range(12)) + "/file.txt")
    assert len(str(long)) >= pathsafe.MAX_PATH
    assert pathsafe.extended_path(long).startswith("\\\\?\\")


def test_extended_path_is_idempotent(windows_only: None) -> None:
    long = Path("C:/" + "/".join("d" * 30 for _ in range(12)) + "/file.txt")
    once = pathsafe.extended_path(long)
    assert pathsafe.extended_path(Path(once)) == once


def test_is_within_compares_resolved_paths(workspace: Path) -> None:
    assert pathsafe.is_within(workspace / "docs" / "notes.md", workspace)
    assert pathsafe.is_within(workspace, workspace)
    assert not pathsafe.is_within(workspace.parent, workspace)


def test_unicode_filenames_are_preserved(workspace: Path) -> None:
    """Unicode must survive resolution intact -- no mangling to ASCII."""
    target = workspace / "docs" / "rapport-café-naïve.txt"
    resolved = pathsafe.resolve_path(target, workspaces=(workspace,))
    assert resolved.exists
    assert "café" in str(resolved.resolved)
    assert "naïve" in str(resolved.resolved)


# --- Junctions (the Windows-specific escape) --------------------------------


def _make_junction(link: Path, target: Path) -> bool:
    """Create a directory junction. Returns False if the OS refuses.

    Junctions do not need elevation on Windows, unlike symlinks -- which is exactly
    why they are the realistic escape vector worth testing.
    """
    try:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            timeout=15,
            check=False,
        )
        return result.returncode == 0 and link.exists()
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.fixture
def junction_escape(workspace: Path, tmp_path: Path, windows_only: None) -> tuple[Path, Path]:
    """A junction inside the workspace pointing at a secret directory outside it."""
    secret = tmp_path / "outside-secret"
    secret.mkdir()
    (secret / "passwords.txt").write_text("hunter2\n", encoding="utf-8")

    link = workspace / "innocent-looking"
    if not _make_junction(link, secret):
        pytest.skip("could not create a junction in this environment")
    return link, secret


def test_junction_is_detected_as_a_reparse_point(junction_escape: tuple[Path, Path]) -> None:
    link, _ = junction_escape
    assert pathsafe.is_reparse_point(link)


def test_junction_escape_is_detected(junction_escape: tuple[Path, Path], workspace: Path) -> None:
    """The path *looks* inside the workspace but resolves outside it."""
    link, _secret = junction_escape
    target = link / "passwords.txt"

    assert str(target).startswith(str(workspace))  # looks contained

    resolved = pathsafe.resolve_path(target, workspaces=(workspace,))
    assert resolved.inside_workspace is None  # is not
    assert resolved.traversed_reparse_point is not None
    assert resolved.escaped


def test_engine_denies_a_junction_escape(
    junction_escape: tuple[Path, Path], config: Config, paths: AppPaths
) -> None:
    link, _ = junction_escape
    config.safety.follow_symlinks = False
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(request_for("read_file", paths=[link / "passwords.txt"]))
    assert decision.effect is Effect.DENY
    assert decision.stage == "junction_escape"


def test_follow_symlinks_opt_in_permits_the_escape(
    junction_escape: tuple[Path, Path], config: Config, paths: AppPaths
) -> None:
    """The setting exists and works; it is off by default and doctor flags it."""
    link, _ = junction_escape
    config.safety.follow_symlinks = True
    engine = PermissionEngine(config, paths)
    decision = engine.evaluate(request_for("read_file", paths=[link / "passwords.txt"]))
    assert decision.stage != "junction_escape"


def test_walk_does_not_follow_junctions_by_default(
    junction_escape: tuple[Path, Path], workspace: Path
) -> None:
    """A recursive scan must not wander out of the workspace through a link."""
    from localai.tools.fs_read import _walk

    visited = [
        Path(entry.path)
        for _, entry, _ in _walk(
            workspace,
            max_depth=10,
            max_entries=5000,
            excludes=frozenset(),
            follow_links=False,
        )
    ]
    assert not any("passwords.txt" == p.name for p in visited)


def test_walk_terminates_on_a_self_referential_junction(
    workspace: Path, windows_only: None
) -> None:
    """A junction pointing at its own ancestor must not cause an infinite walk."""
    from localai.tools.fs_read import _walk

    loop = workspace / "docs" / "loop"
    if not _make_junction(loop, workspace):
        pytest.skip("could not create a junction in this environment")

    count = sum(
        1
        for _ in _walk(
            workspace, max_depth=20, max_entries=3000, excludes=frozenset(), follow_links=True
        )
    )
    assert count < 3000, "walk did not terminate; the loop guard failed"


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
def test_drive_relative_path_is_made_absolute(workspace: Path) -> None:
    resolved = pathsafe.resolve_path("readme.txt", workspaces=(workspace,), base=workspace)
    assert resolved.resolved.is_absolute()
