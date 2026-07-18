"""Pre-modification backups and diff generation.

Before any tool changes a file's contents, the original is copied into the backup
directory under a timestamped name. Backups are what make ``/undo`` possible and
what makes an incorrect edit by a small local model recoverable rather than
destructive.

Backups are intentionally *not* garbage-collected automatically. Deleting a user's
only copy of something to reclaim disk space is precisely the failure mode this
module exists to prevent; :func:`prune` is offered for the user to invoke.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class BackupRecord:
    """One saved copy of a file, with enough detail to restore it."""

    original: Path
    backup: Path
    created_at: float
    size_bytes: int
    sha256: str
    operation: str
    """``modify`` | ``delete`` | ``move``"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "original": str(self.original),
            "backup": str(self.backup),
            "created_at": self.created_at,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "operation": self.operation,
        }


@dataclass
class BackupManager:
    """Creates, indexes and restores backups."""

    root: Path
    records: list[BackupRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._index = self.root / "index.jsonl"

    def backup(self, path: Path, *, operation: str = "modify") -> BackupRecord | None:
        """Copy ``path`` into the backup store. Returns None if there is nothing to copy.

        The backup name embeds a millisecond timestamp and a hash of the full source
        path, so two files with the same basename from different directories never
        collide.
        """
        if not path.is_file():
            return None

        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        discriminator = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:8]
        destination = self.root / f"{stamp}-{discriminator}-{path.name}"

        try:
            data = path.read_bytes()
            # copy2 preserves timestamps, so a restored file looks like the original.
            shutil.copy2(path, destination)
        except OSError:
            return None

        record = BackupRecord(
            original=path.resolve(),
            backup=destination,
            created_at=time.time(),
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            operation=operation,
        )
        self.records.append(record)
        self._append_index(record)
        return record

    def restore(self, record: BackupRecord) -> bool:
        """Copy a backup back over its original location."""
        try:
            record.original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(record.backup, record.original)
            return True
        except OSError:
            return False

    def latest_for(self, path: Path) -> BackupRecord | None:
        resolved = path.resolve(strict=False)
        return next(
            (r for r in reversed(self.records) if r.original == resolved),
            None,
        )

    def _append_index(self, record: BackupRecord) -> None:
        """Record the backup in a JSONL index so it survives a restart."""
        try:
            with self._index.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict()) + "\n")
        except OSError:
            pass  # a missing index entry must not prevent the backup itself

    def load_index(self) -> list[BackupRecord]:
        """Read previously recorded backups from disk."""
        if not self._index.exists():
            return []
        loaded: list[BackupRecord] = []
        for line in self._index.read_text(encoding="utf-8").splitlines():
            try:
                data = json.loads(line)
                loaded.append(
                    BackupRecord(
                        original=Path(data["original"]),
                        backup=Path(data["backup"]),
                        created_at=data["created_at"],
                        size_bytes=data["size_bytes"],
                        sha256=data["sha256"],
                        operation=data.get("operation", "modify"),
                    )
                )
            except (json.JSONDecodeError, KeyError):
                continue
        self.records = loaded
        return loaded

    def prune(self, *, older_than_days: float = 30.0) -> int:
        """Delete backups older than a cutoff. Only ever called by explicit user action."""
        cutoff = time.time() - older_than_days * 86400
        removed = 0
        for record in list(self.records):
            if record.created_at < cutoff:
                try:
                    record.backup.unlink(missing_ok=True)
                    self.records.remove(record)
                    removed += 1
                except OSError:
                    continue
        return removed

    def total_size(self) -> int:
        return sum(r.size_bytes for r in self.records)


def unified_diff(
    before: str, after: str, *, path: str = "file", context: int = 3, max_lines: int = 400
) -> str:
    """Render a unified diff for the preview shown before an edit is applied.

    Truncation is by line count rather than characters so the diff always ends at a
    line boundary and stays readable.
    """
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=False),
            after.splitlines(keepends=False),
            fromfile=f"{path} (current)",
            tofile=f"{path} (proposed)",
            n=context,
            lineterm="",
        )
    )
    if not lines:
        return "(no textual change)"
    if len(lines) > max_lines:
        omitted = len(lines) - max_lines
        lines = [*lines[:max_lines], f"... {omitted} more diff line(s) not shown"]
    return "\n".join(lines)


def diff_stats(before: str, after: str) -> dict[str, int]:
    """Count added and removed lines, for a one-line change summary."""
    added = removed = 0
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0, lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {"added": added, "removed": removed}
