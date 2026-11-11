"""Test doubles shared by the test modules (not itself a test module)."""
from __future__ import annotations

from datetime import datetime, timezone

from nudge.connectors import gsheets
from nudge.connectors.gsheets import HEADERS, SheetsClient
from nudge.engine.gaps import Gap

UTC = timezone.utc


def dt(hour: int, minute: int = 0, day: int = 8) -> datetime:
    """September 2026, UTC. dt(16) is Tuesday noon in New York."""
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


class FakeSheets(SheetsClient):
    """Overrides only the transport, so the real parsing is under test."""

    def __init__(self, grid: list[list[str]] | None = None) -> None:
        super().__init__(service=object())
        self.grid = grid if grid is not None else [list(HEADERS)]
        self.created = 0

    def create(self, title: str = "") -> str:
        self.created += 1
        return "sheet1"

    def fetch_grid(self, spreadsheet_id: str) -> list[list[str]]:
        return [list(row) for row in self.grid]

    @staticmethod
    def _cell(ref: str) -> tuple[int, int]:
        letters = "".join(c for c in ref if c.isalpha())
        number = int("".join(c for c in ref if c.isdigit()))
        column = 0
        for char in letters:
            column = column * 26 + (ord(char.upper()) - 64)
        return number - 1, column - 1

    def update_cells(self, spreadsheet_id, updates):
        for ref, value in updates:
            row, column = self._cell(ref)
            while len(self.grid) <= row:
                self.grid.append([])
            while len(self.grid[row]) <= column:
                self.grid[row].append("")
            self.grid[row][column] = str(value)

    def write_values(self, spreadsheet_id, anchor, values):
        self.grid = [list(v) for v in values]

    # --- helpers for tests ---------------------------------------------
    def add(self, name, course="CS 4262", due="", expected="", percent="",
            repeat="", not_before="", sheet_id=""):
        """Placed by canonical column order, so adding a column can't skew it."""
        values = {
            "name": name, "course": course, "due": due, "expected": expected,
            "percent": percent, "repeat": repeat, "not_before": not_before,
            "sheet_id": sheet_id,
        }
        self.grid.append([values[key] for key in gsheets.CANONICAL])

    def row(self, name: str) -> dict[str, str]:
        header = self.grid[0]
        name_col = next(i for i, h in enumerate(header) if h == "Assignment")
        for row in self.grid[1:]:
            if len(row) > name_col and row[name_col] == name:
                return {h: (row[i] if i < len(row) else "") for i, h in enumerate(header)}
        raise AssertionError(f"no row named {name!r} in {self.grid}")


class FakeGaps:
    """A calendar with one fixed free stretch."""

    free_intervals = None  # the capacity check falls back to the flat estimate

    def __init__(self, gap: Gap | None) -> None:
        self.gap = gap

    def refresh(self, now: datetime) -> None:
        pass

    def current_gap(self, now: datetime) -> Gap | None:
        return self.gap if self.gap and self.gap.contains(now) else None
