"""Recycle Bin deletion via the Win32 shell API.

Deleting to the Recycle Bin rather than unlinking is the difference between a
mistake being annoying and a mistake being unrecoverable, so it is the default.

This is implemented with ``ctypes`` against ``SHFileOperationW`` rather than by
adding the ``send2trash`` dependency. The call is about forty lines, has no
transitive dependencies, and keeps a security-relevant behaviour inside code we
review ourselves. On non-Windows platforms :func:`recycle` reports that it is
unavailable and the caller falls back to an explicit permanent delete, which
requires its own confirmation.

The critical detail is the double-NUL terminator: ``SHFileOperationW`` takes a
*list* of paths separated by NUL and terminated by a second NUL. Passing a normally
terminated string makes it read past the buffer.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

WINDOWS = sys.platform == "win32"

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
    """True when Recycle Bin deletion can be used on this platform."""
    return WINDOWS


def recycle(path: Path) -> RecycleResult:
    """Send ``path`` to the Recycle Bin. Never raises."""
    if not WINDOWS:
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
