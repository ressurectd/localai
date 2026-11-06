"""Reversible deletion: the Windows Recycle Bin and the freedesktop.org Trash.

Deleting to the Recycle Bin rather than unlinking is the difference between a
mistake being annoying and a mistake being unrecoverable, so it is the default.

**Windows** uses ``ctypes`` against ``SHFileOperationW`` rather than adding the
``send2trash`` dependency. The call is about forty lines, has no transitive
dependencies, and keeps a security-relevant behaviour inside code we review
ourselves. The critical detail is the double-NUL terminator: ``SHFileOperationW``
takes a *list* of paths separated by NUL and terminated by a second NUL. Passing a
normally terminated string makes it read past the buffer.

**Linux and other freedesktop.org systems** implement the Trash specification
(https://specifications.freedesktop.org/trash-spec/latest/) directly, for the same
reason. Items land where GNOME Files, Dolphin and ``gio trash --restore`` can find
and restore them. Points worth stating:

* The ``.trashinfo`` file is created with ``O_EXCL`` *before* the item is moved. That
  file is the lock that reserves a name, as the specification requires, so two
  concurrent deletes can never pick the same name and overwrite each other.
* The item is moved with ``rename``, which is atomic and never copies. An item on a
  different filesystem from the home trash goes to that filesystem's own
  ``$topdir/.Trash-$uid``. If neither works the delete is refused rather than
  falling back to a copy-then-delete that could fail halfway.
* A symlink is trashed as a link. Its target is never followed or moved.

**macOS** reports the Trash as unavailable: Finder's "Put Back" depends on metadata
only Finder writes. The caller then requires an explicit, confirmed permanent delete.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

WINDOWS = sys.platform == "win32"
FREEDESKTOP = not WINDOWS and sys.platform != "darwin"

FO_DELETE = 0x0003
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_NOERRORUI = 0x0400
FOF_SILENT = 0x0004


@dataclass(frozen=True, slots=True)
class RecycleResult:
    ok: bool
    detail: str
    aborted: bool = False


def available() -> bool:
    """True when reversible deletion can be used on this platform."""
    return WINDOWS or FREEDESKTOP


def bin_name() -> str:
    """What this platform calls the place deleted items go, for user-facing text."""
    return "Recycle Bin" if WINDOWS else "Trash"


def recycle(path: Path) -> RecycleResult:
    """Send ``path`` to the Recycle Bin or Trash. Never raises."""
    if WINDOWS:
        return _recycle_windows(path)
    if FREEDESKTOP:
        try:
            return _trash_freedesktop(path)
        except OSError as exc:
            return RecycleResult(False, f"cannot move {path} to the Trash: {exc}")
    return RecycleResult(False, "reversible deletion is not supported on this platform")


def _recycle_windows(path: Path) -> RecycleResult:
    if sys.platform != "win32":  # narrows the platform so the type checker accepts windll
        return RecycleResult(False, "the Recycle Bin is only available on Windows")
    if not path.exists():
        return RecycleResult(False, f"{path} does not exist")

    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    # SHFileOperationW rejects relative paths and does not understand the \\?\
    # extended prefix, so we pass a plain absolute path.
    target = str(path.resolve(strict=False))
    if target.startswith("\\\\?\\"):
        target = target[4:]

    operation = SHFILEOPSTRUCTW(
        hwnd=None,
        wFunc=FO_DELETE,
        pFrom=target + "\0\0",  # double-NUL terminated list of one path
        pTo=None,
        fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT,
        fAnyOperationsAborted=False,
        hNameMappings=None,
        lpszProgressTitle=None,
    )

    try:
        shell32 = ctypes.windll.shell32
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
        shell32.SHFileOperationW.restype = ctypes.c_int
        code = shell32.SHFileOperationW(ctypes.byref(operation))
    except (AttributeError, OSError) as exc:
        return RecycleResult(False, f"SHFileOperationW unavailable: {exc}")

    if operation.fAnyOperationsAborted:
        return RecycleResult(False, "the operation was aborted", aborted=True)
    if code != 0:
        return RecycleResult(False, f"{_ERRORS.get(code, 'shell error')} (code {code})")
    return RecycleResult(True, f"moved {path.name} to the Recycle Bin")


#: The documented SHFileOperation error codes worth naming. Others are reported numerically.
_ERRORS = {
    0x71: "the source and destination are the same file",
    0x74: "the source is a root directory and cannot be deleted",
    0x75: "the operation was cancelled by the user",
    0x78: "access denied: the path is protected or in use",
    0x7C: "the path is invalid",
    0x10000: "an unspecified shell error occurred",
}


# --- freedesktop.org Trash ----------------------------------------------------------


def home_trash() -> Path:
    """``$XDG_DATA_HOME/Trash``, defaulting to ``~/.local/share/Trash``."""
    data_home = os.environ.get("XDG_DATA_HOME", "")
    # The XDG spec says a relative value is invalid and must be ignored.
    base = Path(data_home) if Path(data_home).is_absolute() else Path.home() / ".local" / "share"
    return base / "Trash"


def _mount_point(path: Path) -> Path:
    """The top directory of the filesystem holding ``path`` (``$topdir`` in the spec)."""
    device = path.lstat().st_dev
    current = path
    while current.parent != current:
        try:
            if current.parent.lstat().st_dev != device:
                break
        except OSError:
            break
        current = current.parent
    return current


def _ensure_trash_dirs(trash: Path) -> None:
    for sub in (trash, trash / "files", trash / "info"):
        sub.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = sub.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError(f"{sub} is not a real directory")


def _topdir_trash(topdir: Path) -> Path | None:
    """The trash directory to use on a filesystem other than the home one.

    Prefers an administrator-created ``$topdir/.Trash/$uid``, which the spec only
    permits when ``.Trash`` is a real, sticky directory -- a check that stops another
    user planting a symlink to redirect our deletes. Otherwise ``$topdir/.Trash-$uid``.
    """
    if sys.platform == "win32":  # unreachable; also narrows the type checker's platform
        return None
    uid = os.getuid()
    shared = topdir / ".Trash"
    try:
        info = shared.lstat()
        if stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX:
            candidate = shared / str(uid)
            _ensure_trash_dirs(candidate)
            return candidate
    except OSError:
        pass
    candidate = topdir / f".Trash-{uid}"
    try:
        _ensure_trash_dirs(candidate)
        if candidate.lstat().st_uid != uid:
            return None
    except OSError:
        return None
    return candidate


def _trash_freedesktop(path: Path) -> RecycleResult:
    # Absolute, with the parent resolved but the final component left alone, so a
    # symlink is trashed as itself rather than its target.
    absolute = Path(os.path.abspath(path))  # noqa: PTH100 - resolve() would follow the link
    if not os.path.lexists(absolute):
        return RecycleResult(False, f"{path} does not exist")
    if absolute.parent == absolute:
        return RecycleResult(False, "refusing to trash the filesystem root")
    target = absolute.parent.resolve() / absolute.name

    home = home_trash()
    resolved_home = home.resolve()
    if target == resolved_home or target in resolved_home.parents:
        return RecycleResult(False, f"refusing to trash {target}: it contains the Trash itself")
    if resolved_home in target.parents:
        return RecycleResult(False, f"{target} is already in the Trash")

    device = target.lstat().st_dev
    home_device = _existing_ancestor(home).stat().st_dev
    if device == home_device:
        trash = home
        _ensure_trash_dirs(trash)
        recorded = str(target)
    else:
        topdir = _mount_point(target)
        found = _topdir_trash(topdir)
        if found is None:
            return RecycleResult(
                False,
                f"{target} is on a different filesystem from your Trash and that "
                f"filesystem has no usable trash directory ({topdir})",
            )
        trash = found
        # The spec allows, and recommends, a path relative to $topdir here, so the
        # item can still be restored if the volume is mounted somewhere else.
        recorded = str(target.relative_to(topdir))

    info_file, name = _reserve_name(trash, target.name, recorded)
    try:
        target.rename(trash / "files" / name)
    except OSError:
        info_file.unlink(missing_ok=True)
        raise
    return RecycleResult(True, f"moved {target.name} to the Trash ({trash})")


def _existing_ancestor(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _reserve_name(trash: Path, original: str, recorded: str) -> tuple[Path, str]:
    """Atomically create ``info/<name>.trashinfo`` for an unused name and return it."""
    content = (
        "[Trash Info]\n"
        f"Path={quote(recorded, safe='/')}\n"
        f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n"
    ).encode()
    # "report.tar.gz" becomes "report.1.tar.gz"; a dotfile keeps its leading dot.
    head, dot, tail = original[1:].partition(".")
    stem, suffix = original[0] + head, dot + tail
    for attempt in range(10_000):
        name = original if attempt == 0 else f"{stem}.{attempt}{suffix}"
        info_file = trash / "info" / f"{name}.trashinfo"
        try:
            fd = os.open(info_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        if os.path.lexists(trash / "files" / name):
            # An orphaned item with no info file. Keep the reservation out of its way.
            info_file.unlink()
            continue
        return info_file, name
    raise OSError(f"could not find a free name for {original} in {trash}")
