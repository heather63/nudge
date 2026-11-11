"""The Google Sheet: the one place assignments live.

nudge reads it fresh on every tick and writes back only three things: the
% done after you report progress, the next due date when a repeating row rolls
over, and an ID for any row that doesn't have one. Everything else is yours.

SQLite holds no copy of any of this — only scheduling state (snoozes, declines,
the offers log), keyed by the row's ID.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from . import config
from . import recurrence
from . import timeutil as tu
from .connectors import gsheets
from .connectors.gsheets import SheetsClient, column_letter
from .store.repo import Repo

log = logging.getLogger(__name__)


def remaining_minutes(expected_min: int | None, percent_done: float) -> float:
    """What is left of an assignment, in minutes."""
    expected = expected_min or config.DEFAULT_EXPECTED_MINUTES
    return max(expected * (1.0 - (percent_done or 0.0) / 100.0), 0.0)


@dataclass(slots=True)
class Assignment:
    """One row of the sheet."""
    sheet_id: str
    name: str
    course: str | None = None
    due_at: datetime | None = None
    expected_min: int | None = None
    percent_done: float = 0.0
    repeat_rule: str | None = None
    not_before: str | None = None
    available_from: datetime | None = None

    @property
    def remaining_min(self) -> float:
        return remaining_minutes(self.expected_min, self.percent_done)

    @property
    def done(self) -> bool:
        return self.percent_done >= 100

    @property
    def label(self) -> str:
        return f"{self.course} · {self.name}" if self.course else self.name


class Sheet:
    def __init__(self, repo: Repo, client: SheetsClient | None = None) -> None:
        self.repo = repo
        self.client = client or SheetsClient()

    # --- the spreadsheet itself ------------------------------------------
    def spreadsheet_id(self, create: bool = True) -> str | None:
        existing = self.repo.kv_get(gsheets.SPREADSHEET_ID)
        if existing or not create:
            return existing
        spreadsheet_id = self.client.create()
        self.repo.kv_set(gsheets.SPREADSHEET_ID, spreadsheet_id)
        log.info("created spreadsheet %s", self.client.url(spreadsheet_id))
        return spreadsheet_id

    @property
    def url(self) -> str | None:
        spreadsheet_id = self.spreadsheet_id(create=False)
        return self.client.url(spreadsheet_id) if spreadsheet_id else None

    # --- reading ---------------------------------------------------------
    def read(self, now: datetime | None = None) -> list[Assignment]:
        """Every assignment in the sheet, with repeating rows rolled forward."""
        now = now or tu.now_utc()
        spreadsheet_id = self.spreadsheet_id()
        index, rows = self.client.read(spreadsheet_id)
        index = self._ensure_columns(spreadsheet_id, index)
        if "name" not in index:  # column 0 is falsy, so test membership
            log.warning("the sheet has no recognisable 'Assignment' column")
            return []

        writes: list[tuple[str, object]] = []
        seen: set[str] = set()
        assignments: list[Assignment] = []

        for row in rows:
            name = row.get("name")
            if not name:
                continue
            sheet_id = row.get("sheet_id")
            # A blank ID gets one; a copy-pasted row carries the original's ID
            # and gets its own, so the two don't share snoozes and offers.
            if not sheet_id or sheet_id in seen:
                sheet_id = gsheets.new_sheet_id()
                if "sheet_id" in index:
                    writes.append((self._cell(index, "sheet_id", row.row_number), sheet_id))
            seen.add(sheet_id)

            assignment = Assignment(
                sheet_id=sheet_id,
                name=name,
                course=row.get("course") or None,
                due_at=gsheets.parse_date(row.get("due"), now=now),
                expected_min=gsheets.parse_duration(row.get("expected")),
                percent_done=gsheets.parse_percent(row.get("percent")),
                repeat_rule=row.get("repeat") or None,
                not_before=row.get("not_before") or None,
            )
            if self._roll(assignment, now):
                writes.append((self._cell(index, "due", row.row_number),
                               gsheets.format_date(assignment.due_at)))
                writes.append((self._cell(index, "percent", row.row_number), 0))
            assignment.available_from = recurrence.resolve_not_before(
                recurrence.parse_not_before(assignment.not_before), assignment.due_at
            )
            assignments.append(assignment)

        self.client.update_cells(spreadsheet_id, writes)
        return assignments

    def _roll(self, assignment: Assignment, now: datetime) -> bool:
        """Move a repeating assignment on to its next occurrence.

        Once it is finished — or once its due date has passed, because a weekly
        quiz you missed is simply gone — the row takes the next date and starts
        again at 0%. The offers log keeps the history.
        """
        days = recurrence.parse_rule(assignment.repeat_rule)
        if not days:
            return False
        due_at = assignment.due_at
        if due_at is not None and due_at > now and not assignment.done:
            return False
        if assignment.done and due_at is not None and due_at > now:
            # Finished this week's early: the next one is after the date just
            # completed, not after today.
            next_at = recurrence.next_due(days, due_at)
        else:
            next_at = recurrence.due_now(days, now)
        assignment.due_at = next_at
        assignment.percent_done = 0.0
        self.repo.clear_state(assignment.sheet_id)
        log.info("rolled %s to %s", assignment.label, gsheets.format_date(next_at))
        return True

    def _ensure_columns(self, spreadsheet_id, index: dict[str, int]) -> dict[str, int]:
        """Add any column the sheet is missing. Existing columns keep their places."""
        missing = [name for name in gsheets.CANONICAL if name not in index]
        if not missing:
            return index
        if len(missing) == len(gsheets.CANONICAL):
            # Nothing matched at all. Stamping a fresh header over a sheet whose
            # header simply failed to read would shift every column by one.
            log.error("no recognisable header row; leaving the sheet alone")
            return index
        index = dict(index)
        position = max(index.values()) + 1
        writes = []
        for name in missing:
            index[name] = position
            writes.append((f"{column_letter(position)}1", gsheets.header_for(name)))
            position += 1
        self.client.update_cells(spreadsheet_id, writes)
        log.info("added sheet columns: %s", ", ".join(w[1] for w in writes))
        return index

    @staticmethod
    def _cell(index: dict[str, int], name: str, row_number: int) -> str:
        return f"{column_letter(index[name])}{row_number}"

    # --- writing ---------------------------------------------------------
    def write_percent(self, sheet_id: str, percent: float) -> bool:
        """Set one row's % done. Re-reads first, since rows may have moved."""
        spreadsheet_id = self.spreadsheet_id()
        index, rows = self.client.read(spreadsheet_id)
        if "percent" not in index:
            return False
        for row in rows:
            if row.get("sheet_id") == sheet_id:
                self.client.update_cells(
                    spreadsheet_id,
                    [(self._cell(index, "percent", row.row_number),
                      gsheets.format_percent(percent))],
                )
                return True
        log.warning("row %s is no longer in the sheet", sheet_id)
        return False
