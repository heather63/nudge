"""Connection and migrations. The only place that knows where the file lives."""
from __future__ import annotations

import atexit
import logging
import sqlite3
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = Path(path or config.DB_PATH)
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    # Every CLI command is its own short-lived process. Without an explicit
    # checkpoint the committed data can sit in the -wal file, and an orphaned
    # WAL takes the whole mirror with it.
    atexit.register(close, conn)
    return conn


def close(conn: sqlite3.Connection) -> None:
    """Fold the WAL back into the database file, then close. Safe to call twice."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except sqlite3.ProgrammingError:
        pass  # already closed
    except sqlite3.Error:
        log.warning("could not checkpoint the database on exit", exc_info=True)


ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("assignments", "repeat_rule", "TEXT"),
    ("assignments", "not_before", "TEXT"),
    ("assignments", "available_from", "TEXT"),
]

# Tables from the chunk-based model, before the sheet became the source of
# truth. They are dropped on first open; `offers` changed shape too, so a
# non-empty log is kept aside rather than silently discarded.
LEGACY_TABLES = ("chunks", "chunk_state")


def migrate(conn: sqlite3.Connection) -> None:
    _retire_chunk_model(conn)
    conn.executescript(SCHEMA_PATH.read_text())
    for table, column, decl in ADDED_COLUMNS:
        _add_column(conn, table, column, decl)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _retire_chunk_model(conn: sqlite3.Connection) -> None:
    tables = _tables(conn)
    if "chunks" not in tables and "offers" not in tables:
        return  # fresh database

    if "offers" in tables and "chunk_id" in _columns(conn, "offers"):
        count = conn.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"]
        if count:
            log.warning(
                "offers log has %d chunk-era rows; keeping them as offers_legacy", count
            )
            conn.execute("DROP TABLE IF EXISTS offers_legacy")
            conn.execute("ALTER TABLE offers RENAME TO offers_legacy")
        else:
            conn.execute("DROP TABLE offers")

    if "assignments" in tables and "name" not in _columns(conn, "assignments"):
        # The old table keyed on folder_name/title. The sheet re-seeds it.
        log.warning("rebuilding the assignments mirror for the sheet model")
        for table in ("assignment_state", "documents", "proposals", "assignments"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("DELETE FROM kv WHERE key LIKE 'tasks_sync_cursor%'")

    for table in LEGACY_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    existing = _columns(conn, table)
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
