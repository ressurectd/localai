"""Read-only filesystem tools.

All of these are ``RiskLevel.READ`` and non-mutating, so in Auto mode they run
without confirmation -- which makes their bounds the thing that matters. Every walk
is bounded by depth, entry count and time; every read is bounded by bytes and
streamed rather than slurped. An unbounded recursive scan of a 4 TB mechanical drive
is a denial-of-service against the user's own machine.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import re
import stat
import time
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from localai.config.models import RiskLevel
from localai.safety import pathsafe
from localai.tools.base import Tool, ToolContext, ToolResult
from localai.tools.runner import path_from

#: Directories skipped by default during recursive walks. Each is either enormous,
#: uninteresting, or actively hostile to traversal (reparse loops under AppData).
DEFAULT_EXCLUDES = frozenset(
    {
        "$RECYCLE.BIN",
        "System Volume Information",
        "node_modules",
        ".git",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".gradle",
        ".idea",
        ".vs",
        "Windows",
        "WinSxS",
        "Program Files",
        "Program Files (x86)",
    }
)

_BINARY_SNIFF = 8192


def _humanise(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _looks_binary(path: Path) -> bool:
    """Heuristic: a NUL byte in the first 8 KiB means binary.

    Cheap, and correct for essentially every text format. Getting this wrong in the
    permissive direction would push mojibake into the model's context.
    """
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(_BINARY_SNIFF)
    except OSError:
        return False


def _decode(raw: bytes) -> tuple[str, str]:
    """Decode bytes, returning ``(text, encoding)``.

    UTF-8 first, then UTF-16 when a BOM is present (common for PowerShell output and
    Windows-authored files), then cp1252 as a lossy last resort. Unicode filenames
    and content must survive intact, so we never silently drop to ASCII.
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace"), "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "cp1252"


def _walk(
    root: Path,
    *,
    max_depth: int,
    max_entries: int,
    excludes: frozenset[str],
    follow_links: bool,
    deadline: float | None = None,
) -> Iterator[tuple[Path, os.DirEntry[str], int]]:
    """Bounded, non-recursive directory walk.

    Iterative rather than recursive so a deep tree cannot exhaust the Python stack,
    and it yields as it goes so callers can stop early. ``PermissionError`` on one
    directory skips that directory only -- a single unreadable folder must not abort
    a scan of an entire drive.
    """
    stack: list[tuple[Path, int]] = [(root, 0)]
    seen_dirs: set[str] = set()  # resolved paths already descended into
    yielded = 0

    while stack and yielded < max_entries:
        directory, depth = stack.pop()
        if deadline and time.monotonic() > deadline:
            return
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if yielded >= max_entries:
                        return
                    try:
                        is_dir = entry.is_dir(follow_symlinks=follow_links)
                    except OSError:
                        continue

                    yield directory, entry, depth
                    yielded += 1

                    if not is_dir or depth >= max_depth or entry.name in excludes:
                        continue
                    if not follow_links and pathsafe.is_reparse_point(Path(entry.path)):
                        continue

                    # Cycle detection, needed only when following links: with
                    # follow_links off we skip reparse points outright, so no cycle
                    # can form. The key is the resolved path, NOT (st_dev, st_ino):
                    # DirEntry.stat() on Windows returns cached FindFirstFile data
                    # in which st_ino and st_dev are 0 for every entry, so an inode
                    # key would make all directories look identical and collapse the
                    # walk to a single level.
                    if follow_links:
                        try:
                            real = os.path.normcase(os.path.realpath(entry.path))
                        except OSError:
                            real = os.path.normcase(entry.path)
                        if real in seen_dirs:
                            continue
                        seen_dirs.add(real)

                    stack.append((Path(entry.path), depth + 1))
        except (PermissionError, OSError):
            continue


class ListDirectory(Tool):
    name = "list_directory"
    description = (
        "List the files and subdirectories directly inside a directory (not recursive). "
        "Returns name, type, size and modification time. Use this to explore before "
        "reading files."
    )
    category = "filesystem"
    risk = RiskLevel.READ
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list. Defaults to the cwd."},
            "pattern": {"type": "string", "description": "Optional glob filter, e.g. '*.pdf'."},
            "include_hidden": {"type": "boolean", "default": False},
            "sort_by": {"type": "string", "enum": ["name", "size", "modified"], "default": "name"},
            "limit": {"type": "integer", "default": 200, "minimum": 1, "maximum": 5000},
        },
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "path")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        target = arguments.get("path", ".")
        pattern = arguments.get("pattern")
        return f"list {target}" + (f" matching {pattern}" if pattern else "")

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        target = Path(arguments.get("path") or context.cwd)
        if not target.is_absolute():
            target = context.cwd / target

        if not target.exists():
            return ToolResult.failure(f"{target} does not exist")
        if not target.is_dir():
            return ToolResult.failure(f"{target} is a file, not a directory. Use read_file.")

        pattern = arguments.get("pattern")
        include_hidden = arguments.get("include_hidden", False)
        limit = arguments.get("limit", 200)

        rows: list[tuple[str, str, int, float]] = []
        truncated = False
        try:
            with os.scandir(target) as entries:
                for entry in entries:
                    if not include_hidden and entry.name.startswith("."):
                        continue
                    if pattern and not fnmatch.fnmatch(entry.name, pattern):
                        continue
                    if len(rows) >= limit:
                        truncated = True
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                        kind = "dir" if entry.is_dir(follow_symlinks=False) else "file"
                        if pathsafe.is_reparse_point(Path(entry.path)):
                            kind = "link"
                        rows.append((entry.name, kind, info.st_size, info.st_mtime))
                    except OSError:
                        rows.append((entry.name, "?", 0, 0.0))
        except PermissionError:
            return ToolResult.failure(
                f"access denied reading {target}. Try a directory you own, or run as "
                "administrator if this is intentional."
            )
        except OSError as exc:
            return ToolResult.failure(f"cannot read {target}: {exc}")

        key = {
            "name": lambda r: r[0].lower(),
            "size": lambda r: -r[2],
            "modified": lambda r: -r[3],
        }[arguments.get("sort_by", "name")]
        rows.sort(key=lambda r: (r[1] != "dir", key(r)))  # directories first

        lines = [f"{target}  ({len(rows)} entries)"]
        for name, kind, size, mtime in rows:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else "-"
            marker = {"dir": "/", "link": "@", "?": "?"}.get(kind, "")
            size_text = "-" if kind == "dir" else _humanise(size)
            lines.append(f"  {stamp}  {size_text:>10}  {name}{marker}")
        if truncated:
            lines.append(f"  ... limited to {limit} entries; narrow with 'pattern'")

        return ToolResult(
            content="\n".join(lines),
            metadata={
                "path": str(target),
                "count": len(rows),
                "directories": sum(1 for r in rows if r[1] == "dir"),
                "files": sum(1 for r in rows if r[1] == "file"),
                "truncated": truncated,
            },
        )


class FindFiles(Tool):
    name = "find_files"
    description = (
        "Search for files and directories by name recursively. Supports glob patterns "
        "such as '*.pdf' or 'invoice*'. Returns matching paths with sizes. Use this to "
        "locate files when you do not know where they are."
    )
    category = "filesystem"
    risk = RiskLevel.READ
    parameters = {
        "type": "object",
        "properties": {
            "root": {"type": "string", "description": "Directory to search from."},
            "pattern": {"type": "string", "description": "Glob matched against the file name."},
            "max_depth": {"type": "integer", "default": 12, "minimum": 1, "maximum": 40},
            "max_results": {"type": "integer", "default": 200, "minimum": 1, "maximum": 5000},
            "include_dirs": {"type": "boolean", "default": False},
            "min_size_bytes": {"type": "integer", "default": 0, "minimum": 0},
            "modified_after": {
                "type": "string",
                "description": "ISO date (YYYY-MM-DD); only files modified on or after it.",
            },
        },
        "required": ["pattern"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "root")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        return f"find {arguments.get('pattern')!r} under {arguments.get('root', '.')}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        root = Path(arguments.get("root") or context.cwd)
        if not root.is_absolute():
            root = context.cwd / root
        if not root.exists():
            return ToolResult.failure(f"{root} does not exist")

        pattern = arguments["pattern"]
        max_results = arguments.get("max_results", 200)
        safety = context.config.safety
        cutoff = 0.0
        if raw_date := arguments.get("modified_after"):
            try:
                cutoff = time.mktime(time.strptime(raw_date, "%Y-%m-%d"))
            except ValueError:
                return ToolResult.failure(f"modified_after must be YYYY-MM-DD, got {raw_date!r}")

        matches: list[tuple[Path, int, float]] = []
        scanned = 0
        deadline = time.monotonic() + safety.tool_timeout_s * 0.8

        def scan() -> bool:
            nonlocal scanned
            for _, entry, _ in _walk(
                root,
                max_depth=min(arguments.get("max_depth", 12), safety.max_scan_depth),
                max_entries=safety.max_scan_entries,
                excludes=DEFAULT_EXCLUDES,
                follow_links=safety.follow_symlinks,
                deadline=deadline,
            ):
                scanned += 1
                if context.cancelled():
                    return False
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir and not arguments.get("include_dirs", False):
                    continue
                if not fnmatch.fnmatch(entry.name.lower(), pattern.lower()):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if info.st_size < arguments.get("min_size_bytes", 0) and not is_dir:
                    continue
                if cutoff and info.st_mtime < cutoff:
                    continue
                matches.append((Path(entry.path), info.st_size, info.st_mtime))
                if len(matches) >= max_results:
                    return True
            return True

        # Walking the filesystem is blocking; a thread keeps the event loop (and
        # therefore streaming and cancellation) responsive during a long scan.
        completed = await asyncio.to_thread(scan)
        if not completed:
            return ToolResult.failure("cancelled")

        if not matches:
            return ToolResult(
                content=f"No files matching {pattern!r} under {root} ({scanned:,} entries scanned).",
                metadata={"matches": 0, "scanned": scanned},
            )

        matches.sort(key=lambda m: -m[2])
        lines = [f"{len(matches)} match(es) for {pattern!r} under {root}:"]
        lines += [
            f"  {time.strftime('%Y-%m-%d', time.localtime(mtime))}  {_humanise(size):>10}  {path}"
            for path, size, mtime in matches
        ]
        if len(matches) >= max_results:
            lines.append(f"  ... stopped at {max_results} results")

        return ToolResult(
            content="\n".join(lines),
            metadata={
                "matches": len(matches),
                "scanned": scanned,
                "paths": [str(p) for p, _, _ in matches[:500]],
            },
        )


class SearchFileContents(Tool):
    name = "search_file_contents"
    description = (
        "Search inside text files for a regular expression, returning matching lines "
        "with their file path and line number. Use this to find where something is "
        "mentioned across many files."
    )
    category = "filesystem"
    risk = RiskLevel.READ
    returns_untrusted_content = True
    parameters = {
        "type": "object",
        "properties": {
            "root": {"type": "string"},
            "query": {"type": "string", "description": "Regular expression to search for."},
            "file_pattern": {"type": "string", "default": "*", "description": "Glob filter."},
            "ignore_case": {"type": "boolean", "default": True},
            "max_results": {"type": "integer", "default": 100, "minimum": 1, "maximum": 2000},
            "context_lines": {"type": "integer", "default": 0, "minimum": 0, "maximum": 5},
            "max_depth": {"type": "integer", "default": 12, "minimum": 1, "maximum": 40},
        },
        "required": ["query"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "root")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        return (
            f"search for {arguments.get('query')!r} in "
            f"{arguments.get('file_pattern', '*')} under {arguments.get('root', '.')}"
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        root = Path(arguments.get("root") or context.cwd)
        if not root.is_absolute():
            root = context.cwd / root
        if not root.exists():
            return ToolResult.failure(f"{root} does not exist")

        try:
            regex = re.compile(
                arguments["query"], re.IGNORECASE if arguments.get("ignore_case", True) else 0
            )
        except re.error as exc:
            return ToolResult.failure(
                f"invalid regular expression {arguments['query']!r}: {exc}. "
                "Escape special characters such as ( ) [ ] * + ? with a backslash."
            )

        safety = context.config.safety
        max_results = arguments.get("max_results", 100)
        context_lines = arguments.get("context_lines", 0)
        file_pattern = arguments.get("file_pattern", "*")
        deadline = time.monotonic() + safety.tool_timeout_s * 0.8

        hits: list[str] = []
        files_searched = 0
        files_skipped = 0

        def search() -> None:
            nonlocal files_searched, files_skipped
            for _, entry, _ in _walk(
                root,
                max_depth=min(arguments.get("max_depth", 12), safety.max_scan_depth),
                max_entries=safety.max_scan_entries,
                excludes=DEFAULT_EXCLUDES,
                follow_links=safety.follow_symlinks,
                deadline=deadline,
            ):
                if len(hits) >= max_results or context.cancelled():
                    return
                try:
                    if entry.is_dir(follow_symlinks=False):
                        continue
                    if not fnmatch.fnmatch(entry.name.lower(), file_pattern.lower()):
                        continue
                    if entry.stat(follow_symlinks=False).st_size > safety.max_read_bytes:
                        files_skipped += 1
                        continue
                except OSError:
                    continue

                path = Path(entry.path)
                if _looks_binary(path):
                    files_skipped += 1
                    continue

                try:
                    # Reading line by line keeps memory flat regardless of file size.
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        window: list[str] = []
                        for number, line in enumerate(handle, start=1):
                            if regex.search(line):
                                for offset, previous in enumerate(window):
                                    hits.append(
                                        f"{path}:{number - len(window) + offset}- {previous.rstrip()}"
                                    )
                                hits.append(f"{path}:{number}: {line.rstrip()[:400]}")
                                if len(hits) >= max_results:
                                    return
                            if context_lines:
                                window = [*window, line][-context_lines:]
                    files_searched += 1
                except (OSError, UnicodeError):
                    files_skipped += 1

        await asyncio.to_thread(search)
        if context.cancelled():
            return ToolResult.failure("cancelled")

        if not hits:
            return ToolResult(
                content=(
                    f"No matches for {arguments['query']!r} in {files_searched:,} file(s) "
                    f"under {root}."
                ),
                metadata={
                    "matches": 0,
                    "files_searched": files_searched,
                    "files_skipped": files_skipped,
                },
            )

        header = f"{len(hits)} match(es) in {files_searched:,} file(s):"
        return ToolResult(
            content="\n".join([header, *hits]),
            untrusted=True,
            metadata={
                "matches": len(hits),
                "files_searched": files_searched,
                "files_skipped": files_skipped,
            },
        )


class ReadFile(Tool):
    name = "read_file"
    description = (
        "Read the contents of a text file. Supports reading a specific line range for "
        "large files. Returns the text with line numbers. Binary files are refused."
    )
    category = "filesystem"
    risk = RiskLevel.READ
    returns_untrusted_content = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File to read."},
            "start_line": {"type": "integer", "default": 1, "minimum": 1},
            "max_lines": {"type": "integer", "default": 500, "minimum": 1, "maximum": 20000},
            "line_numbers": {"type": "boolean", "default": True},
        },
        "required": ["path"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "path")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        start = arguments.get("start_line", 1)
        span = f" lines {start}-{start + arguments.get('max_lines', 500) - 1}" if start > 1 else ""
        return f"read {arguments.get('path')}{span}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = Path(arguments["path"])
        if not path.is_absolute():
            path = context.cwd / path

        if not path.exists():
            return ToolResult.failure(
                f"{path} does not exist. Use find_files to locate it, or list_directory "
                "to see what is there."
            )
        if path.is_dir():
            return ToolResult.failure(f"{path} is a directory. Use list_directory instead.")

        try:
            size = path.stat().st_size
        except OSError as exc:
            return ToolResult.failure(f"cannot stat {path}: {exc}")

        if _looks_binary(path):
            return ToolResult.failure(
                f"{path} appears to be binary ({_humanise(size)}). Use inspect_metadata for "
                "details, or file_hash to identify it."
            )

        start = arguments.get("start_line", 1)
        max_lines = arguments.get("max_lines", 500)
        cap = context.config.safety.max_read_bytes

        def read() -> tuple[list[str], int, str, bool]:
            """Stream the requested window without loading the whole file."""
            collected: list[str] = []
            total = 0
            encoding = "utf-8"
            with path.open("rb") as raw:
                head = raw.read(4)
                raw.seek(0)
                if head.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
                    encoding = _decode(head)[1]
            with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
                consumed = 0
                for number, line in enumerate(handle, start=1):
                    total = number
                    if number < start:
                        continue
                    if len(collected) < max_lines and consumed < cap:
                        collected.append(line.rstrip("\r\n"))
                        consumed += len(line)
            return collected, total, encoding, total > start + max_lines - 1

        try:
            lines, total_lines, encoding, more = await asyncio.to_thread(read)
        except PermissionError:
            return ToolResult.failure(f"access denied reading {path}")
        except OSError as exc:
            return ToolResult.failure(f"cannot read {path}: {exc}")

        if not lines:
            note = (
                f"(file has {total_lines} lines; start_line={start} is past the end)"
                if total_lines
                else "(file is empty)"
            )
            return ToolResult(content=note, metadata={"path": str(path), "lines": total_lines})

        width = len(str(start + len(lines) - 1))
        body = (
            "\n".join(f"{start + i:>{width}} | {line}" for i, line in enumerate(lines))
            if arguments.get("line_numbers", True)
            else "\n".join(lines)
        )
        header = f"{path} ({_humanise(size)}, {total_lines} lines, {encoding})"
        footer = (
            f"\n... {total_lines - start - len(lines) + 1} more lines. "
            f"Read them with start_line={start + len(lines)}."
            if more
            else ""
        )
        return ToolResult(
            content=f"{header}\n{body}{footer}",
            untrusted=True,
            metadata={
                "path": str(path),
                "size_bytes": size,
                "total_lines": total_lines,
                "returned_lines": len(lines),
                "encoding": encoding,
                "has_more": more,
            },
        )


class InspectMetadata(Tool):
    name = "inspect_metadata"
    description = (
        "Get detailed metadata for a file or directory: size, timestamps, attributes, "
        "whether it is a link, and for directories the immediate item count."
    )
    category = "filesystem"
    risk = RiskLevel.READ
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "path")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        return f"inspect {arguments.get('path')}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = Path(arguments["path"])
        if not path.is_absolute():
            path = context.cwd / path
        if not path.exists() and not path.is_symlink():
            return ToolResult.failure(f"{path} does not exist")

        try:
            info = path.lstat()
        except OSError as exc:
            return ToolResult.failure(f"cannot stat {path}: {exc}")

        is_link = pathsafe.is_reparse_point(path)
        details: dict[str, Any] = {
            "path": str(path),
            "resolved": str(path.resolve(strict=False)),
            "type": "directory" if path.is_dir() else "file",
            "size_bytes": info.st_size,
            "size_human": _humanise(info.st_size),
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.st_mtime)),
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.st_ctime)),
            "accessed": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.st_atime)),
            "readonly": not os.access(path, os.W_OK),
            "is_link": is_link,
            "mode": stat.filemode(info.st_mode),
        }
        if is_link:
            try:
                details["link_target"] = str(path.readlink())
            except OSError:
                details["link_target"] = str(path.resolve(strict=False))
        if path.is_dir():
            try:
                with os.scandir(path) as entries:
                    items = list(entries)
                details["item_count"] = len(items)
            except OSError:
                details["item_count"] = None
        else:
            details["suffix"] = path.suffix

        body = "\n".join(f"  {k:<14} {v}" for k, v in details.items() if v is not None)
        return ToolResult(content=f"{path}\n{body}", metadata=details)


class FileHash(Tool):
    name = "file_hash"
    description = (
        "Compute a cryptographic hash of a file (sha256 by default). Use this to verify "
        "integrity or to confirm two files are identical."
    )
    category = "filesystem"
    risk = RiskLevel.READ
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "algorithm": {
                "type": "string",
                "enum": ["md5", "sha1", "sha256", "sha512"],
                "default": "sha256",
            },
        },
        "required": ["path"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "path")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        return f"{arguments.get('algorithm', 'sha256')} of {arguments.get('path')}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = Path(arguments["path"])
        if not path.is_absolute():
            path = context.cwd / path
        if not path.is_file():
            return ToolResult.failure(f"{path} is not a file")

        algorithm = arguments.get("algorithm", "sha256")

        def digest() -> tuple[str, int]:
            hasher = hashlib.new(algorithm)
            total = 0
            # 1 MiB chunks: large enough to be efficient on mechanical drives,
            # small enough that a multi-gigabyte file never lands in RAM.
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    hasher.update(chunk)
                    total += len(chunk)
            return hasher.hexdigest(), total

        try:
            value, size = await asyncio.to_thread(digest)
        except PermissionError:
            return ToolResult.failure(f"access denied reading {path}")
        except OSError as exc:
            return ToolResult.failure(f"cannot read {path}: {exc}")

        return ToolResult(
            content=f"{algorithm}({path.name}) = {value}\nsize: {_humanise(size)}",
            metadata={"path": str(path), "algorithm": algorithm, "hash": value, "size_bytes": size},
        )


class FindDuplicates(Tool):
    name = "find_duplicates"
    description = (
        "Find duplicate files under a directory by comparing content hashes. Groups "
        "identical files together and reports the space that could be reclaimed."
    )
    category = "filesystem"
    risk = RiskLevel.READ
    parameters = {
        "type": "object",
        "properties": {
            "root": {"type": "string"},
            "min_size_bytes": {"type": "integer", "default": 4096, "minimum": 1},
            "max_files": {"type": "integer", "default": 5000, "minimum": 1, "maximum": 100000},
            "max_depth": {"type": "integer", "default": 12, "minimum": 1, "maximum": 40},
        },
        "required": ["root"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "root")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        return f"find duplicate files under {arguments.get('root')}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        root = Path(arguments["root"])
        if not root.is_absolute():
            root = context.cwd / root
        if not root.is_dir():
            return ToolResult.failure(f"{root} is not a directory")

        safety = context.config.safety
        min_size = arguments.get("min_size_bytes", 4096)
        max_files = arguments.get("max_files", 5000)
        deadline = time.monotonic() + safety.tool_timeout_s * 0.85

        def find() -> tuple[dict[str, list[Path]], int, int]:
            """Two-pass: group by size, then hash only within size collisions.

            Hashing every file would be pointlessly slow -- files of different sizes
            cannot be duplicates. On a mechanical drive this is the difference
            between minutes and hours.
            """
            by_size: dict[int, list[Path]] = defaultdict(list)
            examined = 0
            for _, entry, _ in _walk(
                root,
                max_depth=min(arguments.get("max_depth", 12), safety.max_scan_depth),
                max_entries=safety.max_scan_entries,
                excludes=DEFAULT_EXCLUDES,
                follow_links=False,
                deadline=deadline,
            ):
                if examined >= max_files or context.cancelled():
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        continue
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                if size >= min_size:
                    by_size[size].append(Path(entry.path))
                    examined += 1

            groups: dict[str, list[Path]] = defaultdict(list)
            hashed = 0
            for size, candidates in by_size.items():
                if len(candidates) < 2 or time.monotonic() > deadline:
                    continue
                for candidate in candidates:
                    try:
                        hasher = hashlib.sha256()
                        with candidate.open("rb") as handle:
                            while chunk := handle.read(1024 * 1024):
                                hasher.update(chunk)
                        groups[f"{hasher.hexdigest()}:{size}"].append(candidate)
                        hashed += 1
                    except OSError:
                        continue
            return {k: v for k, v in groups.items() if len(v) > 1}, examined, hashed

        duplicates, examined, hashed = await asyncio.to_thread(find)
        if context.cancelled():
            return ToolResult.failure("cancelled")

        if not duplicates:
            return ToolResult(
                content=f"No duplicates found among {examined:,} files under {root}.",
                metadata={"groups": 0, "examined": examined},
            )

        reclaimable = 0
        lines = [f"{len(duplicates)} duplicate group(s) among {examined:,} files:"]
        for key, members in sorted(duplicates.items(), key=lambda kv: -len(kv[1]))[:50]:
            size = int(key.split(":")[1])
            reclaimable += size * (len(members) - 1)
            lines.append(f"\n  {len(members)} copies, {_humanise(size)} each:")
            lines += [f"    {p}" for p in members[:10]]
            if len(members) > 10:
                lines.append(f"    ... and {len(members) - 10} more")

        lines.append(f"\nReclaimable if deduplicated: {_humanise(reclaimable)}")
        lines.append("(This tool only reports. Nothing has been deleted.)")
        return ToolResult(
            content="\n".join(lines),
            metadata={
                "groups": len(duplicates),
                "examined": examined,
                "hashed": hashed,
                "reclaimable_bytes": reclaimable,
            },
        )
