"""Connection and schema. The only place that knows where the file lives."""
from __future__ import annotations

import atexit
import logging
import sqlite3
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Before the sheet became the only copy, SQLite mirrored every assignment.
# Those tables are dropped on first open; only the spreadsheet link is kept.
MIRROR_TABLES = ("offers", "assignment_state", "documents", "proposals", "assignments")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path or config.DB_PATH)
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    _drop_mirror(conn)
    conn.executescript(SCHEMA_PATH.read_text())
    # Every CLI command is its own short-lived process. Without an explicit
    # checkpoint the committed data can sit in the -wal file and be lost.
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


def _drop_mirror(conn: sqlite3.Connection) -> None:
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "assignments" not in tables:
        return
    log.warning("dropping the old assignments mirror; the sheet is the only copy now")
    for table in MIRROR_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("DELETE FROM kv WHERE key != 'spreadsheet_id'")
