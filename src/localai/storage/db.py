"""SQLite connection management and the migration runner.

All database access in the project goes through a :class:`Database` instance; no
module opens ``sqlite3.connect`` itself. That keeps PRAGMA settings, the migration
state and the connection lifetime in one auditable place.

Migrations are plain ``.sql`` files named ``NNN_description.sql`` in
``storage/migrations``. They are applied in numeric order inside a transaction and
recorded in ``schema_migrations``. Migrations are never edited after release -- a
correction ships as a new file. See docs/database-schema.md.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from localai.errors import MigrationError, StorageError

log = logging.getLogger(__name__)

MIGRATION_PACKAGE = "localai.storage.migrations"
_MIGRATION_NAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    requires_fts5: bool = False


def discover_migrations() -> list[Migration]:
    """Load migration files from the package, sorted by version.

    Reading through ``importlib.resources`` rather than a filesystem glob means
    migrations work identically from a wheel, a zipapp or a source checkout.
    """
    found: list[Migration] = []
    for entry in resources.files(MIGRATION_PACKAGE).iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if not match:
            continue
        sql = entry.read_text(encoding="utf-8")
        found.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                sql=sql,
                requires_fts5="fts5" in sql.lower(),
            )
        )

    found.sort(key=lambda m: m.version)
    for index, migration in enumerate(found, start=1):
        if migration.version != index:
            raise MigrationError(
                f"migration versions must be contiguous from 001; found {migration.version:03d} "
                f"at position {index}",
                remediation="Renumber the migration files so there are no gaps or duplicates.",
            )
    return found


def has_fts5(connection: sqlite3.Connection) -> bool:
    """Probe whether this SQLite build includes FTS5."""
    try:
        connection.execute("CREATE VIRTUAL TABLE temp.__fts_probe USING fts5(x)")
        connection.execute("DROP TABLE temp.__fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


class Database:
    """A thread-confined SQLite connection with migrations applied.

    SQLite connections are not safe to share across threads. Textual runs the UI on
    one thread and blocking work on others, so we keep one connection per thread in
    thread-local storage and let SQLite's WAL mode handle concurrency between them.
    """

    def __init__(self, path: Path, *, timeout: float = 15.0) -> None:
        self.path = path
        self._timeout = timeout
        self._local = threading.local()
        self._migration_warnings: list[str] = []
        self.fts_available = False
        path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    @property
    def connection(self) -> sqlite3.Connection:
        """The connection for the calling thread, created on first use."""
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            return existing

        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self._timeout,
                isolation_level=None,  # explicit transactions
            )
        except sqlite3.Error as exc:
            raise StorageError(
                f"cannot open the database at {self.path}: {exc}",
                remediation="Check the file is not locked by another process, and that the "
                "localai home directory is writable.",
            ) from exc

        connection.row_factory = sqlite3.Row
        # WAL lets a reader (the usage panel) run while a writer (the agent loop)
        # commits, which matters because the TUI queries usage during generation.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        self._local.connection = connection
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block in a transaction, rolling back on any exception."""
        connection = self.connection
        connection.execute("BEGIN")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, params)

    def query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, params).fetchone()

    # -- migrations -----------------------------------------------------------

    def applied_versions(self) -> list[int]:
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY,"
            " name TEXT NOT NULL,"
            " applied_at REAL NOT NULL)"
        )
        rows = self.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        return [int(r["version"]) for r in rows]

    def migrate(self) -> list[int]:
        """Apply every pending migration. Returns the versions applied."""
        import time

        applied = set(self.applied_versions())
        fts = has_fts5(self.connection)
        self.fts_available = fts
        newly: list[int] = []

        for migration in discover_migrations():
            if migration.version in applied:
                continue
            if migration.requires_fts5 and not fts:
                warning = (
                    f"migration {migration.version:03d}_{migration.name} needs FTS5, which this "
                    "SQLite build lacks; conversation search will fall back to substring matching"
                )
                self._migration_warnings.append(warning)
                log.warning(warning)
                continue

            # Transaction control lives *inside* the script. sqlite3.executescript
            # commits any open transaction before it runs, so wrapping the call in
            # an outer BEGIN/COMMIT would leave nothing to commit. Splitting the SQL
            # into statements is not an option either: the CREATE TRIGGER bodies in
            # migration 002 contain semicolons. Bundling the bookkeeping INSERT into
            # the same script keeps "schema changed" and "migration recorded" atomic.
            #
            # The interpolated values are safe by construction: `version` is an int
            # and `name` matched ``[a-z0-9_]+`` in _MIGRATION_NAME. There is no
            # parameter binding available to executescript.
            script = (
                "BEGIN;\n"
                f"{migration.sql}\n"
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES "
                f"({migration.version}, '{migration.name}', {time.time()});\n"
                "COMMIT;"
            )
            try:
                self.connection.executescript(script)
            except sqlite3.Error as exc:
                with suppress(sqlite3.Error):
                    self.connection.execute("ROLLBACK")
                raise MigrationError(
                    f"migration {migration.version:03d}_{migration.name} failed: {exc}",
                    remediation="The database is unchanged. Report this with the SQL error above.",
                    version=migration.version,
                ) from exc

            newly.append(migration.version)
            log.info("applied migration %03d_%s", migration.version, migration.name)

        if newly and fts and 2 in newly:
            self._backfill_fts()
        return newly

    def _backfill_fts(self) -> None:
        """Populate the FTS index from messages that predate migration 002."""
        self.connection.execute(
            "INSERT INTO messages_fts (content, thinking, conversation_id, message_id) "
            "SELECT content, thinking, conversation_id, id FROM messages "
            "WHERE id NOT IN (SELECT message_id FROM messages_fts)"
        )

    def migration_status(self) -> dict[str, Any]:
        """Machine-readable migration state for ``localai migrations status --json``."""
        available = discover_migrations()
        applied = self.applied_versions()
        return {
            "database": str(self.path),
            "current_version": max(applied, default=0),
            "latest_version": max((m.version for m in available), default=0),
            "applied": applied,
            "pending": [m.version for m in available if m.version not in set(applied)],
            "migrations": [
                {
                    "version": m.version,
                    "name": m.name,
                    "applied": m.version in set(applied),
                    "requires_fts5": m.requires_fts5,
                }
                for m in available
            ],
            "fts5_available": self.fts_available,
            "warnings": list(self._migration_warnings),
        }

    def health(self) -> dict[str, Any]:
        """Integrity check plus size, for ``ai doctor``."""
        try:
            integrity = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
            page_count = self.connection.execute("PRAGMA page_count").fetchone()[0]
            page_size = self.connection.execute("PRAGMA page_size").fetchone()[0]
            return {
                "ok": integrity == "ok",
                "integrity": integrity,
                "size_bytes": int(page_count) * int(page_size),
                "path": str(self.path),
                "fts5": self.fts_available,
            }
        except sqlite3.Error as exc:
            return {"ok": False, "integrity": str(exc), "path": str(self.path)}

    def close(self) -> None:
        if connection := getattr(self._local, "connection", None):
            connection.close()
            self._local.connection = None
