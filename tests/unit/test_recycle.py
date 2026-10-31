"""The freedesktop.org Trash used for reversible deletes on Linux.

``conftest.isolated_home`` points ``XDG_DATA_HOME`` at a temporary directory, so none
of these tests can touch the real Trash.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

from localai.safety import recycle
from localai.tools.base import ToolContext
from localai.tools.registry import ToolRegistry

pytestmark = [
    pytest.mark.security,
    pytest.mark.skipif(not recycle.FREEDESKTOP, reason="freedesktop.org Trash only"),
]


def _info(trash: Path, name: str) -> dict[str, str]:
    lines = (trash / "info" / f"{name}.trashinfo").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "[Trash Info]"
    return dict(line.split("=", 1) for line in lines[1:])


def test_trash_is_available_and_named_for_the_platform() -> None:
    assert recycle.available()
    assert recycle.bin_name() == "Trash"


def test_home_trash_honours_xdg_data_home(tmp_path: Path) -> None:
    assert recycle.home_trash() == tmp_path / "xdg-data" / "Trash"


def test_relative_xdg_data_home_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """The XDG spec says a relative value is invalid; it must not resolve against cwd."""
    monkeypatch.setenv("XDG_DATA_HOME", "relative/dir")
    assert recycle.home_trash() == Path.home() / ".local" / "share" / "Trash"


def test_file_moves_to_trash_with_restorable_metadata(tmp_path: Path) -> None:
    target = tmp_path / "notes with spaces & ünïcode.txt"
    target.write_text("keep me", encoding="utf-8")

    result = recycle.recycle(target)

    assert result.ok, result.detail
    assert not target.exists()
    trash = recycle.home_trash()
    trashed = trash / "files" / target.name
    assert trashed.read_text(encoding="utf-8") == "keep me"
    info = _info(trash, target.name)
    # Path is percent-encoded, as file managers expect, and decodes to the original.
    assert unquote(info["Path"]) == str(target.resolve())
    assert " " not in info["Path"]
    assert len(info["DeletionDate"]) == len("2026-01-01T00:00:00")


def test_directory_moves_to_trash_whole(tmp_path: Path) -> None:
    folder = tmp_path / "project"
    (folder / "src").mkdir(parents=True)
    (folder / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")

    assert recycle.recycle(folder).ok
    assert not folder.exists()
    assert (recycle.home_trash() / "files" / "project" / "src" / "main.py").exists()


def test_name_collisions_never_overwrite_an_earlier_item(tmp_path: Path) -> None:
    for content in ("first", "second", "third"):
        target = tmp_path / "report.tar.gz"
        target.write_text(content, encoding="utf-8")
        assert recycle.recycle(target).ok

    files = recycle.home_trash() / "files"
    assert sorted(p.name for p in files.iterdir()) == [
        "report.1.tar.gz",
        "report.2.tar.gz",
        "report.tar.gz",
    ]
    assert (files / "report.tar.gz").read_text(encoding="utf-8") == "first"
    assert (files / "report.2.tar.gz").read_text(encoding="utf-8") == "third"
    info = recycle.home_trash() / "info"
    assert len(list(info.iterdir())) == 3


def test_dotfile_collision_keeps_leading_dot(tmp_path: Path) -> None:
    for _ in range(2):
        (tmp_path / ".env").write_text("x", encoding="utf-8")
        assert recycle.recycle(tmp_path / ".env").ok
    names = {p.name for p in (recycle.home_trash() / "files").iterdir()}
    assert names == {".env", ".env.1"}


def test_symlink_is_trashed_as_a_link_not_its_target(tmp_path: Path) -> None:
    """Following the link would move a file the user never named."""
    real = tmp_path / "real.txt"
    real.write_text("precious", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    assert recycle.recycle(link).ok
    assert real.read_text(encoding="utf-8") == "precious"
    assert (recycle.home_trash() / "files" / "link.txt").is_symlink()


def test_dangling_symlink_can_be_trashed(tmp_path: Path) -> None:
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "missing")
    assert recycle.recycle(link).ok
    assert not os.path.lexists(link)


def test_missing_path_is_reported_not_raised(tmp_path: Path) -> None:
    result = recycle.recycle(tmp_path / "nope.txt")
    assert not result.ok
    assert "does not exist" in result.detail


def test_refuses_to_trash_the_trash_or_its_ancestors(tmp_path: Path) -> None:
    trash = recycle.home_trash()
    trash.mkdir(parents=True)
    for target in (trash, trash.parent):
        result = recycle.recycle(target)
        assert not result.ok
        assert "contains the Trash" in result.detail
    assert trash.exists()


def test_refuses_to_trash_an_item_already_in_the_trash(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    assert recycle.recycle(target).ok
    result = recycle.recycle(recycle.home_trash() / "files" / "a.txt")
    assert not result.ok
    assert "already in the Trash" in result.detail


def test_failed_move_leaves_no_orphaned_info_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")

    def refuse(*_: object) -> None:
        raise PermissionError("simulated")

    monkeypatch.setattr(Path, "rename", refuse)
    result = recycle.recycle(target)
    assert not result.ok
    assert target.exists()
    assert list((recycle.home_trash() / "info").iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_trash_directories_are_private(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    assert recycle.recycle(target).ok
    for sub in ("", "files", "info"):
        mode = (recycle.home_trash() / sub).stat().st_mode & 0o777
        assert mode == 0o700, f"{sub or 'Trash'} is {oct(mode)}"


async def test_delete_path_tool_uses_the_trash(
    registry: ToolRegistry, context: ToolContext, workspace: Path
) -> None:
    result = await registry.get("delete_path").run({"path": "readme.txt"}, context)
    assert result.ok, result.error
    assert "Trash" in result.content
    assert result.metadata["method"] == "recycle_bin"
    assert not (workspace / "readme.txt").exists()
    assert (recycle.home_trash() / "files" / "readme.txt").exists()


async def test_delete_path_dry_run_names_the_trash(
    registry: ToolRegistry, context: ToolContext, workspace: Path
) -> None:
    context.dry_run = True
    result = await registry.get("delete_path").run({"path": "readme.txt"}, context)
    assert "Trash" in result.content
    assert (workspace / "readme.txt").exists()


# --- items on another filesystem ----------------------------------------------------


class _OtherDevice:
    """Stands in for the home trash's filesystem, reporting a different device."""

    def stat(self) -> os.stat_result:
        real = Path("/").stat()
        return os.stat_result((*real[:2], real.st_dev + 1, *real[3:]))


def _simulate_other_volume(monkeypatch: pytest.MonkeyPatch, volume: Path) -> None:
    monkeypatch.setattr(recycle, "_existing_ancestor", lambda _p: _OtherDevice())
    monkeypatch.setattr(recycle, "_mount_point", lambda _p: volume)


def test_item_on_another_filesystem_uses_that_volumes_trash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rename() cannot cross filesystems, and copying is not reversible-by-design."""
    volume = tmp_path / "volume"
    (volume / "data").mkdir(parents=True)
    target = volume / "data" / "a.txt"
    target.write_text("x", encoding="utf-8")
    _simulate_other_volume(monkeypatch, volume)

    assert recycle.recycle(target).ok
    trash = volume / f".Trash-{os.getuid()}"
    assert (trash / "files" / "a.txt").exists()
    # Relative to the volume root, so it restores even if mounted elsewhere.
    assert _info(trash, "a.txt")["Path"] == "data/a.txt"
    assert not (recycle.home_trash() / "files").exists()


def test_sticky_shared_trash_is_preferred(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    volume = tmp_path / "volume"
    (volume / ".Trash").mkdir(parents=True)
    (volume / ".Trash").chmod(0o1777)
    target = volume / "a.txt"
    target.write_text("x", encoding="utf-8")
    _simulate_other_volume(monkeypatch, volume)

    assert recycle.recycle(target).ok
    assert (volume / ".Trash" / str(os.getuid()) / "files" / "a.txt").exists()


def test_non_sticky_or_symlinked_shared_trash_is_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another user could plant $topdir/.Trash as a symlink to capture our deletes."""
    volume = tmp_path / "volume"
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    elsewhere.chmod(0o1777)
    volume.mkdir()
    (volume / ".Trash").symlink_to(elsewhere)
    target = volume / "a.txt"
    target.write_text("x", encoding="utf-8")
    _simulate_other_volume(monkeypatch, volume)

    assert recycle.recycle(target).ok
    assert not any(elsewhere.iterdir())
    assert (volume / f".Trash-{os.getuid()}" / "files" / "a.txt").exists()
