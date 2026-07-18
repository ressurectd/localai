"""Filesystem mutation tools.

Every tool here is ``mutating = True``, so the permissions engine requires
confirmation in Manual and Auto modes and refuses outright in read-only mode.
On top of that, each one:

* takes a backup before overwriting or deleting (``safety.backup_before_modify``);
* honours ``safety.dry_run`` by reporting what *would* happen and changing nothing;
* returns a unified diff in its result so the change is reviewable after the fact;
* writes atomically, so an interrupted write cannot truncate the original.

This module is held to strict mypy settings -- see ``pyproject.toml``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from localai.config.models import RiskLevel
from localai.safety import recycle
from localai.safety.backup import BackupManager, diff_stats, unified_diff
from localai.tools.base import Tool, ToolContext, ToolResult
from localai.tools.runner import path_from


def _read_text(path: Path) -> str:
    """Read a file for diffing, tolerating any encoding."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_bytes().decode("cp1252", errors="replace")
    except OSError:
        return ""


def _atomic_write(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write via a temporary file in the same directory, then replace.

    Same directory matters: ``os.replace`` is only atomic within a filesystem, and a
    temp file on another volume would degrade to a copy that can be interrupted.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding=encoding,
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    tmp = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)  # noqa: PTH105 - atomic rename primitive
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _backup_manager(context: ToolContext) -> BackupManager:
    return BackupManager(context.paths.backup_dir)


class WriteFile(Tool):
    name = "write_file"
    description = (
        "Create a new file or replace an existing file's entire contents. Returns a diff "
        "of what changed. The previous version is backed up automatically. To change part "
        "of a file, prefer edit_file."
    )
    category = "filesystem"
    risk = RiskLevel.WRITE
    mutating = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File to write."},
            "content": {"type": "string", "description": "The complete new contents."},
            "create_parents": {"type": "boolean", "default": True},
        },
        "required": ["path", "content"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "path")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        content = arguments.get("content", "")
        path = Path(str(arguments.get("path", "")))
        verb = "overwrite" if path.exists() else "create"
        return f"{verb} {path} ({len(content):,} chars, {content.count(chr(10)) + 1} lines)"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = Path(str(arguments["path"]))
        if not path.is_absolute():
            path = context.cwd / path
        content = str(arguments["content"])

        if path.is_dir():
            return ToolResult.failure(f"{path} is a directory, not a file")

        existed = path.is_file()
        before = _read_text(path) if existed else ""
        diff = unified_diff(before, content, path=str(path))
        stats = diff_stats(before, content)

        if context.dry_run:
            return ToolResult(
                content=(
                    f"[dry run] would {'overwrite' if existed else 'create'} {path}\n"
                    f"+{stats['added']} -{stats['removed']} lines\n\n{diff}"
                ),
                flags=["dry_run"],
                metadata={"path": str(path), "dry_run": True, **stats},
            )

        backup = None
        if existed and context.config.safety.backup_before_modify:
            backup = _backup_manager(context).backup(path, operation="modify")

        try:
            if arguments.get("create_parents", True):
                path.parent.mkdir(parents=True, exist_ok=True)
            elif not path.parent.exists():
                return ToolResult.failure(
                    f"{path.parent} does not exist. Set create_parents to true, or create "
                    "it with create_directory."
                )
            await asyncio.to_thread(_atomic_write, path, content)
        except PermissionError:
            return ToolResult.failure(f"access denied writing {path}")
        except OSError as exc:
            return ToolResult.failure(f"cannot write {path}: {exc}")

        note = f"\nBackup: {backup.backup}" if backup else ""
        return ToolResult(
            content=(
                f"{'Updated' if existed else 'Created'} {path} "
                f"(+{stats['added']} -{stats['removed']} lines){note}\n\n{diff}"
            ),
            metadata={
                "path": str(path),
                "created": not existed,
                "bytes": len(content.encode("utf-8")),
                "backup": str(backup.backup) if backup else None,
                **stats,
            },
        )


class EditFile(Tool):
    name = "edit_file"
    description = (
        "Replace an exact string in a file with new text. The string must appear exactly "
        "once unless replace_all is set. Use this for targeted changes instead of "
        "rewriting a whole file. Returns a diff."
    )
    category = "filesystem"
    risk = RiskLevel.WRITE
    mutating = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {
                "type": "string",
                "description": "Exact text to find, including indentation.",
            },
            "new_text": {"type": "string", "description": "Replacement text."},
            "replace_all": {"type": "boolean", "default": False},
        },
        "required": ["path", "old_text", "new_text"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "path")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        old = str(arguments.get("old_text", ""))
        scope = "all occurrences" if arguments.get("replace_all") else "1 occurrence"
        return f"edit {arguments.get('path')}: replace {scope} of {old[:40]!r}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = Path(str(arguments["path"]))
        if not path.is_absolute():
            path = context.cwd / path
        if not path.is_file():
            return ToolResult.failure(f"{path} does not exist or is not a file")

        old_text = str(arguments["old_text"])
        new_text = str(arguments["new_text"])
        if not old_text:
            return ToolResult.failure(
                "old_text must not be empty; use write_file to replace a file"
            )

        before = _read_text(path)
        occurrences = before.count(old_text)

        # An ambiguous match is reported rather than guessed at. Telling the model how
        # many matches exist lets it supply more surrounding context and retry, which
        # is far better than editing the wrong one.
        if occurrences == 0:
            return ToolResult.failure(
                f"the specified text does not appear in {path}. Read the file first and "
                "copy the exact text, including indentation and line endings."
            )
        if occurrences > 1 and not arguments.get("replace_all", False):
            return ToolResult.failure(
                f"the specified text appears {occurrences} times in {path}. Include more "
                "surrounding context to identify one occurrence uniquely, or set "
                "replace_all to true."
            )

        after = (
            before.replace(old_text, new_text)
            if arguments.get("replace_all", False)
            else before.replace(old_text, new_text, 1)
        )
        if after == before:
            return ToolResult(
                content=f"No change: the replacement text is identical to the original in {path}.",
                metadata={"path": str(path), "changed": False},
            )

        diff = unified_diff(before, after, path=str(path))
        stats = diff_stats(before, after)

        if context.dry_run:
            return ToolResult(
                content=f"[dry run] would edit {path} ({occurrences} match)\n\n{diff}",
                flags=["dry_run"],
                metadata={"path": str(path), "dry_run": True, **stats},
            )

        backup = (
            _backup_manager(context).backup(path, operation="modify")
            if context.config.safety.backup_before_modify
            else None
        )
        try:
            await asyncio.to_thread(_atomic_write, path, after)
        except PermissionError:
            return ToolResult.failure(f"access denied writing {path}")
        except OSError as exc:
            return ToolResult.failure(f"cannot write {path}: {exc}")

        note = f"\nBackup: {backup.backup}" if backup else ""
        return ToolResult(
            content=(
                f"Edited {path}: {occurrences if arguments.get('replace_all') else 1} "
                f"replacement(s), +{stats['added']} -{stats['removed']} lines{note}\n\n{diff}"
            ),
            metadata={
                "path": str(path),
                "replacements": occurrences if arguments.get("replace_all") else 1,
                "backup": str(backup.backup) if backup else None,
                **stats,
            },
        )


class CreateDirectory(Tool):
    name = "create_directory"
    description = "Create a directory, including any missing parent directories."
    category = "filesystem"
    risk = RiskLevel.WRITE
    mutating = True
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "path")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        return f"create directory {arguments.get('path')}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = Path(str(arguments["path"]))
        if not path.is_absolute():
            path = context.cwd / path
        if path.is_dir():
            return ToolResult(content=f"{path} already exists.", metadata={"created": False})
        if context.dry_run:
            return ToolResult(content=f"[dry run] would create directory {path}", flags=["dry_run"])
        try:
            path.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            return ToolResult.failure(f"access denied creating {path}")
        except OSError as exc:
            return ToolResult.failure(f"cannot create {path}: {exc}")
        return ToolResult(content=f"Created {path}", metadata={"path": str(path), "created": True})


class MovePath(Tool):
    name = "move_path"
    description = (
        "Move or rename a file or directory. Refuses to overwrite an existing "
        "destination unless overwrite is set."
    )
    category = "filesystem"
    risk = RiskLevel.WRITE
    mutating = True
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "destination": {"type": "string"},
            "overwrite": {"type": "boolean", "default": False},
        },
        "required": ["source", "destination"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "source", "destination")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        return f"move {arguments.get('source')} -> {arguments.get('destination')}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        source = Path(str(arguments["source"]))
        destination = Path(str(arguments["destination"]))
        if not source.is_absolute():
            source = context.cwd / source
        if not destination.is_absolute():
            destination = context.cwd / destination

        if not source.exists():
            return ToolResult.failure(f"{source} does not exist")
        # Moving a directory into itself creates an unrecoverable mess; refuse early.
        if source.is_dir() and destination.resolve(strict=False).is_relative_to(
            source.resolve(strict=False)
        ):
            return ToolResult.failure(f"cannot move {source} into itself")
        if destination.exists() and not arguments.get("overwrite", False):
            return ToolResult.failure(
                f"{destination} already exists. Set overwrite to true to replace it."
            )
        if context.dry_run:
            return ToolResult(
                content=f"[dry run] would move {source} to {destination}", flags=["dry_run"]
            )

        backup = None
        if destination.is_file() and context.config.safety.backup_before_modify:
            backup = _backup_manager(context).backup(destination, operation="move")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, str(source), str(destination))
        except PermissionError:
            return ToolResult.failure(f"access denied moving {source}")
        except OSError as exc:
            return ToolResult.failure(f"cannot move {source}: {exc}")

        note = f" (previous destination backed up to {backup.backup})" if backup else ""
        return ToolResult(
            content=f"Moved {source} -> {destination}{note}",
            metadata={"source": str(source), "destination": str(destination)},
        )


class CopyPath(Tool):
    name = "copy_path"
    description = "Copy a file or directory to a new location."
    category = "filesystem"
    risk = RiskLevel.WRITE
    mutating = True
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "destination": {"type": "string"},
            "overwrite": {"type": "boolean", "default": False},
        },
        "required": ["source", "destination"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "source", "destination")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        return f"copy {arguments.get('source')} -> {arguments.get('destination')}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        source = Path(str(arguments["source"]))
        destination = Path(str(arguments["destination"]))
        if not source.is_absolute():
            source = context.cwd / source
        if not destination.is_absolute():
            destination = context.cwd / destination

        if not source.exists():
            return ToolResult.failure(f"{source} does not exist")
        if destination.exists() and not arguments.get("overwrite", False):
            return ToolResult.failure(
                f"{destination} already exists. Set overwrite to true to replace it."
            )
        if context.dry_run:
            return ToolResult(
                content=f"[dry run] would copy {source} to {destination}", flags=["dry_run"]
            )

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                await asyncio.to_thread(
                    shutil.copytree, str(source), str(destination), dirs_exist_ok=True
                )
            else:
                await asyncio.to_thread(shutil.copy2, str(source), str(destination))
        except PermissionError:
            return ToolResult.failure(f"access denied copying {source}")
        except OSError as exc:
            return ToolResult.failure(f"cannot copy {source}: {exc}")

        return ToolResult(
            content=f"Copied {source} -> {destination}",
            metadata={"source": str(source), "destination": str(destination)},
        )


class DeletePath(Tool):
    name = "delete_path"
    description = (
        "Delete a file or directory. By default it goes to the Recycle Bin so it can be "
        "restored. Permanent deletion requires permanent=true and is not reversible."
    )
    category = "filesystem"
    risk = RiskLevel.DESTRUCTIVE
    mutating = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "permanent": {
                "type": "boolean",
                "default": False,
                "description": "Bypass the Recycle Bin. Not reversible.",
            },
            "recursive": {
                "type": "boolean",
                "default": False,
                "description": "Required to delete a non-empty directory.",
            },
        },
        "required": ["path"],
    }

    def affected_paths(self, arguments: dict[str, Any]) -> list[Path]:
        return path_from(arguments, "path")

    def describe_call(self, arguments: dict[str, Any]) -> str:
        method = "PERMANENTLY DELETE" if arguments.get("permanent") else "move to Recycle Bin"
        scope = " (recursive)" if arguments.get("recursive") else ""
        return f"{method}: {arguments.get('path')}{scope}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = Path(str(arguments["path"]))
        if not path.is_absolute():
            path = context.cwd / path
        if not path.exists() and not path.is_symlink():
            return ToolResult.failure(f"{path} does not exist")

        # Deleting a drive root or the user profile is never what was meant.
        resolved = path.resolve(strict=False)
        if resolved.parent == resolved or resolved == Path.home():
            return ToolResult.failure(
                f"refusing to delete {resolved}: it is a drive root or your home directory"
            )

        is_dir = path.is_dir()
        recursive = bool(arguments.get("recursive", False))
        if is_dir and not recursive:
            try:
                if any(path.iterdir()):
                    return ToolResult.failure(
                        f"{path} is not empty. Set recursive to true to delete it and "
                        "everything inside it."
                    )
            except OSError as exc:
                return ToolResult.failure(f"cannot inspect {path}: {exc}")

        permanent = bool(arguments.get("permanent", False))
        use_bin = context.config.safety.use_recycle_bin and not permanent

        if context.dry_run:
            method = "move to the Recycle Bin" if use_bin else "permanently delete"
            return ToolResult(content=f"[dry run] would {method} {path}", flags=["dry_run"])

        backup = None
        if path.is_file() and context.config.safety.backup_before_modify:
            backup = _backup_manager(context).backup(path, operation="delete")

        if use_bin:
            outcome = await asyncio.to_thread(recycle.recycle, path)
            if outcome.ok:
                return ToolResult(
                    content=f"Moved {path} to the Recycle Bin. It can be restored from there.",
                    metadata={"path": str(path), "method": "recycle_bin"},
                )
            if not recycle.available():
                return ToolResult.failure(
                    f"the Recycle Bin is unavailable on this platform ({outcome.detail}). "
                    "Set permanent=true to delete irreversibly, if that is what you intend."
                )
            return ToolResult.failure(
                f"could not move {path} to the Recycle Bin: {outcome.detail}. "
                "Set permanent=true to delete irreversibly, if that is what you intend."
            )

        try:
            if is_dir:
                await asyncio.to_thread(shutil.rmtree, str(path))
            else:
                path.unlink()
        except PermissionError:
            return ToolResult.failure(
                f"access denied deleting {path}. The file may be open in another program."
            )
        except OSError as exc:
            return ToolResult.failure(f"cannot delete {path}: {exc}")

        note = f" A backup was kept at {backup.backup}." if backup else ""
        return ToolResult(
            content=f"Permanently deleted {path}. This cannot be undone.{note}",
            flags=["destructive", "permanent"],
            metadata={
                "path": str(path),
                "method": "permanent",
                "backup": str(backup.backup) if backup else None,
            },
        )
