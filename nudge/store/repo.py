"""Typed accessors for scheduling state. No SQL lives outside this module."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import timeutil as tu


@dataclass(slots=True)
class State:
    snooze_until: datetime | None = None
    decline_count: int = 0


@dataclass(slots=True)
class Offer:
    id: int
    sheet_id: str
    label: str
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

    # --- per-assignment state ------------------------------------------
    def states(self) -> dict[str, State]:
        return {
            row["sheet_id"]: State(tu.parse(row["snooze_until"]), int(row["decline_count"]))
            for row in self.conn.execute("SELECT * FROM state")
        }

    def state(self, sheet_id: str) -> State:
        return self.states().get(sheet_id, State())

    def _ensure(self, sheet_id: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO state(sheet_id) VALUES(?)", (sheet_id,))

    def set_snooze(self, sheet_id: str, until: datetime) -> None:
        self._ensure(sheet_id)
        self.conn.execute(
            "UPDATE state SET snooze_until = ? WHERE sheet_id = ?", (tu.iso(until), sheet_id)
        )

    def bump_decline(self, sheet_id: str) -> int:
        self._ensure(sheet_id)
        self.conn.execute(
            "UPDATE state SET decline_count = decline_count + 1 WHERE sheet_id = ?",
            (sheet_id,),
        )
        return self.state(sheet_id).decline_count

    def reset_declines(self, sheet_id: str) -> None:
        self.conn.execute("UPDATE state SET decline_count = 0 WHERE sheet_id = ?", (sheet_id,))

    def clear_state(self, sheet_id: str) -> None:
        self.conn.execute("DELETE FROM state WHERE sheet_id = ?", (sheet_id,))

    # --- offers --------------------------------------------------------
    @staticmethod
    def _offer(row: sqlite3.Row | None) -> Offer | None:
        if row is None:
            return None
        return Offer(
            id=int(row["id"]),
            sheet_id=row["sheet_id"],
            label=row["label"],
            offered_at=tu.parse(row["offered_at"]),
            gap_start=tu.parse(row["gap_start"]),
            gap_end=tu.parse(row["gap_end"]),
            session_min=int(row["session_min"]),
            response=row["response"],
        )

    def record_offer(
        self,
        sheet_id: str,
        label: str,
        gap_start: datetime,
        gap_end: datetime,
        session_min: int,
        now: datetime,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO offers(sheet_id, label, offered_at, gap_start, gap_end, "
            "  session_min) VALUES(?, ?, ?, ?, ?, ?)",
            (sheet_id, label, tu.iso(now), tu.iso(gap_start), tu.iso(gap_end), session_min),
        )
        return int(cur.lastrowid)

    def latest_offer(self, now: datetime, within_hours: float) -> Offer | None:
        cutoff = now - timedelta(hours=within_hours)
        return self._offer(self.conn.execute(
            "SELECT * FROM offers WHERE offered_at >= ? "
            "ORDER BY offered_at DESC, id DESC LIMIT 1",
            (tu.iso(cutoff),),
        ).fetchone())

    def open_offer(self, now: datetime, within_hours: float) -> Offer | None:
        """The latest offer you haven't answered yet, if it is recent."""
        offer = self.latest_offer(now, within_hours)
        return offer if offer and offer.response is None else None

    def resolve_offer(
        self, offer_id: int, response: str, now: datetime, minutes: float | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE offers SET response = ?, minutes_logged = ?, responded_at = ? "
            "WHERE id = ? AND response IS NULL",
            (response, minutes, tu.iso(now), offer_id),
        )

    def expire_open_offers(self, now: datetime, older_than_hours: float) -> None:
        cutoff = now - timedelta(hours=older_than_hours)
        self.conn.execute(
            "UPDATE offers SET response = 'ignored', responded_at = ? "
            "WHERE response IS NULL AND offered_at < ?",
            (tu.iso(now), tu.iso(cutoff)),
        )

    def last_offer_at(self) -> datetime | None:
        row = self.conn.execute("SELECT MAX(offered_at) AS t FROM offers").fetchone()
        return tu.parse(row["t"])

    def offers_today(self, now: datetime) -> int:
        """Derived from the log rather than a counter, so it survives restarts."""
        cutoff = now - timedelta(days=2)
        rows = self.conn.execute(
            "SELECT offered_at FROM offers WHERE offered_at >= ?", (tu.iso(cutoff),)
        ).fetchall()
        today = tu.local_date(now)
        return sum(1 for r in rows if tu.local_date(tu.parse(r["offered_at"])) == today)

    def recent_offers(self, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM offers ORDER BY offered_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
