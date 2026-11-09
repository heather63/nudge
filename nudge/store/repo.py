"""Typed accessors. No SQL lives outside this module."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import config
from .. import timeutil as tu


def remaining_minutes(expected_min: int | None, percent_done: float) -> float:
    """What is left of an assignment, in minutes."""
    expected = expected_min or config.DEFAULT_EXPECTED_MINUTES
    return max(expected * (1.0 - (percent_done or 0.0) / 100.0), 0.0)


@dataclass(slots=True)
class Candidate:
    """One assignment plus everything scoring needs, in a single row."""
    assignment_id: int
    sheet_id: str | None
    name: str
    course: str | None
    kind: str | None
    due_at: datetime | None
    expected_min: int | None
    percent_done: float
    created_at: datetime
    last_offered_at: datetime | None
    offer_count: int
    decline_count: int
    snooze_until: datetime | None
    available_from: datetime | None

    @property
    def remaining_min(self) -> float:
        return remaining_minutes(self.expected_min, self.percent_done)

    @property
    def label(self) -> str:
        return f"{self.course} · {self.name}" if self.course else self.name


@dataclass(slots=True)
class OfferRow:
    id: int
    assignment_id: int
    offered_at: datetime
    gap_start: datetime
    gap_end: datetime
    session_min: int
    response: str | None


class Repo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # --- kv ------------------------------------------------------------
    def kv_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def kv_del(self, key: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE key = ?", (key,))

    def kv_time(self, key: str) -> datetime | None:
        return tu.parse(self.kv_get(key))

    def kv_set_time(self, key: str, when: datetime) -> None:
        self.kv_set(key, tu.iso(when))

    # --- assignments ---------------------------------------------------
    def create_assignment(
        self,
        name: str,
        *,
        sheet_id: str | None = None,
        course: str | None = None,
        kind: str | None = None,
        due_at: datetime | None = None,
        expected_min: int | None = None,
        percent_done: float = 0.0,
        files: str | None = None,
        repeat_rule: str | None = None,
        not_before: str | None = None,
        available_from: datetime | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO assignments(sheet_id, name, course, kind, due_at, "
            "  expected_min, percent_done, files, repeat_rule, not_before, "
            "  available_from, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sheet_id,
                name,
                course,
                kind,
                tu.iso(due_at),
                expected_min,
                percent_done,
                files,
                repeat_rule,
                not_before,
                tu.iso(available_from),
                tu.iso(tu.now_utc()),
            ),
        )
        assignment_id = int(cur.lastrowid)
        self.conn.execute(
            "INSERT OR IGNORE INTO assignment_state(assignment_id) VALUES(?)",
            (assignment_id,),
        )
        return assignment_id

    def update_assignment(self, assignment_id: int, **fields) -> None:
        """Apply named column updates. `dirty` marks it for the outbound push."""
        dirty = fields.pop("dirty", True)
        for key in ("due_at", "available_from"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = tu.iso(fields[key])
        if not fields and not dirty:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values())
        if dirty:
            sets += (", " if sets else "") + "sheet_synced_at = NULL, synced_at = NULL"
        self.conn.execute(
            f"UPDATE assignments SET {sets} WHERE id = ?", (*params, assignment_id)
        )

    def get_assignment(self, assignment_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
        ).fetchone()

    def assignment_by_sheet_id(self, sheet_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM assignments WHERE sheet_id = ?", (sheet_id,)
        ).fetchone()

    def assignment_by_gtask(self, gtask_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM assignments WHERE gtask_id = ?", (gtask_id,)
        ).fetchone()

    def find_assignment(self, course: str | None, name: str) -> sqlite3.Row | None:
        """Fuzzy match on name (and course when given): 'hw 1' finds 'HW1'."""
        return self.conn.execute(
            "SELECT * FROM assignments "
            "WHERE lower(replace(name, ' ', '')) = lower(replace(?, ' ', '')) "
            "  AND (? IS NULL OR lower(replace(COALESCE(course, ''), ' ', '')) "
            "                    = lower(replace(?, ' ', ''))) "
            "  AND status != 'dropped' "
            "ORDER BY id LIMIT 1",
            (name, course, course or ""),
        ).fetchone()

    def all_assignments(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM assignments ORDER BY id").fetchall()

    def active_assignments(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM assignments WHERE status = 'active' "
            "ORDER BY due_at IS NULL, due_at"
        ).fetchall()

    def set_status(self, assignment_id: int, status: str, *, dirty: bool = True) -> None:
        self.update_assignment(assignment_id, status=status, dirty=dirty)

    def set_percent(
        self, assignment_id: int, percent: float, *, dirty: bool = True
    ) -> float:
        """Clamp, store, and close the assignment out at 100%."""
        percent = max(0.0, min(float(percent), 100.0))
        status = "done" if percent >= 100.0 else "active"
        self.update_assignment(
            assignment_id, percent_done=percent, status=status, dirty=dirty
        )
        return percent

    def add_progress(self, assignment_id: int, minutes: float) -> tuple[float, float]:
        """Log time worked. Returns (new percent, minutes still remaining)."""
        row = self.get_assignment(assignment_id)
        expected = row["expected_min"] or config.DEFAULT_EXPECTED_MINUTES
        gained = (minutes / expected) * 100.0 if expected else 100.0
        percent = self.set_percent(assignment_id, (row["percent_done"] or 0) + gained)
        return percent, remaining_minutes(row["expected_min"], percent)

    def candidates(self) -> list[Candidate]:
        rows = self.conn.execute(
            "SELECT a.*, s.last_offered_at, "
            "       COALESCE(s.offer_count, 0) AS offer_count, "
            "       COALESCE(s.decline_count, 0) AS decline_count, "
            "       s.snooze_until "
            "FROM assignments a "
            "LEFT JOIN assignment_state s ON s.assignment_id = a.id "
            "WHERE a.status = 'active' "
            "ORDER BY a.due_at IS NULL, a.due_at"
        ).fetchall()
        return [
            Candidate(
                assignment_id=int(r["id"]),
                sheet_id=r["sheet_id"],
                name=r["name"],
                course=r["course"],
                kind=r["kind"],
                due_at=tu.parse(r["due_at"]),
                expected_min=r["expected_min"],
                percent_done=float(r["percent_done"] or 0),
                created_at=tu.parse(r["created_at"]),
                last_offered_at=tu.parse(r["last_offered_at"]),
                offer_count=int(r["offer_count"]),
                decline_count=int(r["decline_count"]),
                snooze_until=tu.parse(r["snooze_until"]),
                available_from=tu.parse(r["available_from"]),
            )
            for r in rows
        ]

    def summary(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, name, course, kind, due_at, expected_min, percent_done, "
            "       available_from "
            "FROM assignments WHERE status = 'active' "
            "ORDER BY due_at IS NULL, due_at"
        ).fetchall()

    def recurring(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM assignments "
            "WHERE repeat_rule IS NOT NULL AND repeat_rule != '' "
            "  AND status != 'dropped'"
        ).fetchall()

    # --- assignment_state ----------------------------------------------
    def _ensure_state(self, assignment_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO assignment_state(assignment_id) VALUES(?)",
            (assignment_id,),
        )

    def record_offer(
        self, assignment_id: int, gap_start: datetime, gap_end: datetime, session_min: int
    ) -> int:
        now = tu.now_utc()
        self._ensure_state(assignment_id)
        self.conn.execute(
            "UPDATE assignment_state SET last_offered_at = ?, "
            "  offer_count = offer_count + 1 WHERE assignment_id = ?",
            (tu.iso(now), assignment_id),
        )
        cur = self.conn.execute(
            "INSERT INTO offers(assignment_id, offered_at, gap_start, gap_end, "
            "  session_min) VALUES(?, ?, ?, ?, ?)",
            (assignment_id, tu.iso(now), tu.iso(gap_start), tu.iso(gap_end), session_min),
        )
        self.kv_set_time("last_offer_at", now)
        return int(cur.lastrowid)

    def bump_decline(self, assignment_id: int) -> None:
        self._ensure_state(assignment_id)
        self.conn.execute(
            "UPDATE assignment_state SET decline_count = decline_count + 1 "
            "WHERE assignment_id = ?",
            (assignment_id,),
        )

    def reset_declines(self, assignment_id: int) -> None:
        self._ensure_state(assignment_id)
        self.conn.execute(
            "UPDATE assignment_state SET decline_count = 0 WHERE assignment_id = ?",
            (assignment_id,),
        )

    def set_snooze(self, assignment_id: int, until: datetime) -> None:
        self._ensure_state(assignment_id)
        self.conn.execute(
            "UPDATE assignment_state SET snooze_until = ? WHERE assignment_id = ?",
            (tu.iso(until), assignment_id),
        )

    def clear_snooze(self, assignment_id: int) -> None:
        self.conn.execute(
            "UPDATE assignment_state SET snooze_until = NULL WHERE assignment_id = ?",
            (assignment_id,),
        )

    # --- offers --------------------------------------------------------
    def _offer_row(self, row: sqlite3.Row | None) -> OfferRow | None:
        if row is None:
            return None
        return OfferRow(
            id=int(row["id"]),
            assignment_id=int(row["assignment_id"]),
            offered_at=tu.parse(row["offered_at"]),
            gap_start=tu.parse(row["gap_start"]),
            gap_end=tu.parse(row["gap_end"]),
            session_min=int(row["session_min"]),
            response=row["response"],
        )

    def latest_offer(self, *, within_hours: float | None = None) -> OfferRow | None:
        sql = "SELECT * FROM offers"
        params: tuple = ()
        if within_hours is not None:
            cutoff = tu.now_utc() - timedelta(hours=within_hours)
            sql += " WHERE offered_at >= ?"
            params = (tu.iso(cutoff),)
        sql += " ORDER BY offered_at DESC, id DESC LIMIT 1"
        return self._offer_row(self.conn.execute(sql, params).fetchone())

    def resolve_offer(
        self, offer_id: int, response: str, minutes_logged: float | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE offers SET response = ?, minutes_logged = ?, responded_at = ? "
            "WHERE id = ? AND response IS NULL",
            (response, minutes_logged, tu.iso(tu.now_utc()), offer_id),
        )

    def expire_open_offers(self, older_than_hours: float) -> int:
        cutoff = tu.now_utc() - timedelta(hours=older_than_hours)
        cur = self.conn.execute(
            "UPDATE offers SET response = 'ignored', responded_at = ? "
            "WHERE response IS NULL AND offered_at < ?",
            (tu.iso(tu.now_utc()), tu.iso(cutoff)),
        )
        return cur.rowcount

    def offers_today(self) -> int:
        """Derived rather than a kv counter, so it survives restarts and DST."""
        cutoff = tu.now_utc() - timedelta(days=2)
        rows = self.conn.execute(
            "SELECT offered_at FROM offers WHERE offered_at >= ?", (tu.iso(cutoff),)
        ).fetchall()
        today = tu.local_date()
        return sum(1 for r in rows if tu.local_date(tu.parse(r["offered_at"])) == today)

    def last_decline_at(self, assignment_id: int) -> datetime | None:
        row = self.conn.execute(
            "SELECT responded_at FROM offers "
            "WHERE assignment_id = ? AND response = 'not_now' "
            "  AND responded_at IS NOT NULL "
            "ORDER BY responded_at DESC LIMIT 1",
            (assignment_id,),
        ).fetchone()
        return tu.parse(row["responded_at"]) if row else None

    def recent_worked_assignment(self, within_hours: float) -> int | None:
        """Continuity: what were you last actually working on?"""
        cutoff = tu.now_utc() - timedelta(hours=within_hours)
        row = self.conn.execute(
            "SELECT assignment_id FROM offers "
            "WHERE response IN ('done', 'progress') AND responded_at >= ? "
            "ORDER BY responded_at DESC LIMIT 1",
            (tu.iso(cutoff),),
        ).fetchone()
        return int(row["assignment_id"]) if row else None

    def logged_sessions(self, limit: int = 200) -> list[tuple[str, int, float]]:
        """(label, minutes asked for, minutes actually done) for the tuning report."""
        rows = self.conn.execute(
            "SELECT a.name, a.course, o.session_min, o.minutes_logged "
            "FROM offers o JOIN assignments a ON a.id = o.assignment_id "
            "WHERE o.minutes_logged IS NOT NULL "
            "ORDER BY o.responded_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            (
                f"{r['course']} · {r['name']}" if r["course"] else r["name"],
                int(r["session_min"]),
                float(r["minutes_logged"]),
            )
            for r in rows
        ]

    # --- outbound queues ------------------------------------------------
    def tasklist_for_course(self, course: str | None) -> str | None:
        """Where this class's tasks already live, whatever the class is called now."""
        if not course:
            return None
        row = self.conn.execute(
            "SELECT gtasklist_id FROM assignments "
            "WHERE course = ? AND gtasklist_id IS NOT NULL LIMIT 1",
            (course,),
        ).fetchone()
        return row["gtasklist_id"] if row else None

    def previously_synced_count(self) -> int:
        """How many rows have ever reached the sheet. Guards a bad empty read."""
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM assignments WHERE sheet_synced_at IS NOT NULL"
            ).fetchone()["n"]
        )

    def needing_sheet_push(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM assignments WHERE sheet_synced_at IS NULL ORDER BY id"
        ).fetchall()

    def mark_sheet_synced(self, assignment_id: int) -> None:
        self.conn.execute(
            "UPDATE assignments SET sheet_synced_at = ? WHERE id = ?",
            (tu.iso(tu.now_utc()), assignment_id),
        )

    def needing_task_push(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM assignments WHERE synced_at IS NULL AND status != 'dropped' "
            "ORDER BY id"
        ).fetchall()

    def mark_task_dirty(self, assignment_id: int) -> None:
        """Queue an outbound push to Tasks without touching the sheet flag."""
        self.conn.execute(
            "UPDATE assignments SET synced_at = NULL WHERE id = ?", (assignment_id,)
        )

    def mark_task_synced(self, assignment_id: int) -> None:
        self.conn.execute(
            "UPDATE assignments SET synced_at = ? WHERE id = ?",
            (tu.iso(tu.now_utc()), assignment_id),
        )

    def set_gtask(self, assignment_id: int, gtask_id: str, gtasklist_id: str) -> None:
        self.conn.execute(
            "UPDATE assignments SET gtask_id = ?, gtasklist_id = ? WHERE id = ?",
            (gtask_id, gtasklist_id, assignment_id),
        )

    def clear_gtask(self, assignment_id: int) -> None:
        self.conn.execute(
            "UPDATE assignments SET gtask_id = NULL, gtasklist_id = NULL, "
            "synced_at = NULL WHERE id = ?",
            (assignment_id,),
        )

    # --- documents and proposals ----------------------------------------
    def document_sha(self, path: str) -> str | None:
        row = self.conn.execute(
            "SELECT sha256 FROM documents WHERE path = ?", (path,)
        ).fetchone()
        return row["sha256"] if row else None

    def upsert_document(
        self, assignment_id: int, path: str, sha256: str, text: str | None
    ) -> None:
        self.conn.execute(
            "INSERT INTO documents(assignment_id, path, sha256, text_cache, ingested_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "  assignment_id = excluded.assignment_id, sha256 = excluded.sha256, "
            "  text_cache = excluded.text_cache, ingested_at = excluded.ingested_at",
            (assignment_id, path, sha256, text, tu.iso(tu.now_utc())),
        )

    def create_proposal(self, assignment_id: int, payload_json: str) -> int:
        self.conn.execute(
            "UPDATE proposals SET resolved_at = ?, resolution = 'superseded' "
            "WHERE assignment_id = ? AND resolved_at IS NULL",
            (tu.iso(tu.now_utc()), assignment_id),
        )
        cur = self.conn.execute(
            "INSERT INTO proposals(assignment_id, payload_json, created_at) "
            "VALUES(?, ?, ?)",
            (assignment_id, payload_json, tu.iso(tu.now_utc())),
        )
        return int(cur.lastrowid)

    def open_proposal(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT p.*, a.name AS assignment_name, a.course AS course "
            "FROM proposals p JOIN assignments a ON a.id = p.assignment_id "
            "WHERE p.resolved_at IS NULL ORDER BY p.created_at DESC, p.id DESC LIMIT 1"
        ).fetchone()

    def open_proposal_count(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM proposals WHERE resolved_at IS NULL"
            ).fetchone()["n"]
        )

    def resolve_proposal(self, proposal_id: int, resolution: str) -> None:
        self.conn.execute(
            "UPDATE proposals SET resolved_at = ?, resolution = ? WHERE id = ?",
            (tu.iso(tu.now_utc()), resolution, proposal_id),
        )
