"""Google Sheets transport and cell parsing.

Both nudge and you write to the sheet, so writes are per-cell rather than a
whole-range rewrite — a full rewrite would clobber an edit made between the
read and the write.
"""
from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime

from .. import config
from .. import timeutil as tu
from . import google_auth

log = logging.getLogger(__name__)

SHEET_TITLE = "Assignments"
SPREADSHEET_ID = "spreadsheet_id"

# canonical name -> (header text, accepted aliases)
COLUMNS: list[tuple[str, str, tuple[str, ...]]] = [
    ("name", "Assignment", ("assignment", "name", "title", "assignmentname")),
    ("course", "Class", ("class", "course", "subject")),
    ("due", "Due date", ("duedate", "due", "deadline")),
    ("expected", "Expected length", ("expectedlength", "expectedlengthtotal",
                                     "expected", "estimate", "length")),
    ("percent", "% done", ("done", "pctdone", "percentdone", "percent",
                           "completion", "completionpercentage")),
    ("repeat", "Repeats", ("repeats", "repeat", "recurring", "every")),
    ("not_before", "Not before", ("notbefore", "notuntil", "availablefrom",
                                  "earliest", "startsafter", "opens")),
    ("sheet_id", "ID", ("id", "nudgeid", "naggyid")),
]
HEADERS = [header for _, header, _ in COLUMNS]
_HEADER_BY_NAME = {name: header for name, header, _ in COLUMNS}


def header_for(name: str) -> str:
    return _HEADER_BY_NAME[name]


CANONICAL = [name for name, _, _ in COLUMNS]


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def new_sheet_id() -> str:
    """Always starts with a letter.

    A bare hex id is a trap: Sheets reads "202e65" as scientific notation and
    "01191655" as a number with the leading zero stripped, so the id comes back
    mangled and the row looks brand new on the next sync.
    """
    return "id" + secrets.token_hex(4)


# --- cell parsing --------------------------------------------------------
_DURATION = re.compile(
    r"^\s*(?:(?P<h>\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*"
    r"(?:(?P<m>\d+(?:\.\d+)?)\s*m(?:in(?:utes?)?)?)?\s*$",
    re.I,
)


def parse_duration(value: str | None) -> int | None:
    """'3h', '90m', '1h30', '2.5h' -> minutes. A bare number means hours."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    bare = re.fullmatch(r"\d+(?:\.\d+)?", text)
    if bare:
        return int(round(float(text) * 60))
    match = _DURATION.match(text)
    if not match or not (match.group("h") or match.group("m")):
        # "1h30" with no trailing unit
        compact = re.fullmatch(r"\s*(\d+)\s*h\s*(\d+)\s*", text, re.I)
        if compact:
            return int(compact.group(1)) * 60 + int(compact.group(2))
        log.warning("could not read expected length %r", value)
        return None
    hours = float(match.group("h") or 0)
    minutes = float(match.group("m") or 0)
    total = hours * 60 + minutes
    return int(round(total)) or None


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d %Y", "%b %d", "%B %d")


def parse_date(value: str | None, *, now: datetime | None = None) -> datetime | None:
    """Due dates are dates, not instants — end of the local day."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    year = tu.to_local(now or tu.now_utc()).year
    for fmt in _DATE_FORMATS:
        # "Sep 17" has no year: supply this one, so Feb 29 can parse too.
        attempt, pattern = (text, fmt) if "%Y" in fmt or "%y" in fmt else (
            f"{text} {year}", f"{fmt} %Y")
        try:
            parsed = datetime.strptime(attempt, pattern)
        except ValueError:
            continue
        return datetime(
            parsed.year, parsed.month, parsed.day, 23, 59, tzinfo=config.TZ
        ).astimezone(tu.UTC)
    try:  # ISO with a time component
        return tu.parse(text)
    except ValueError:
        log.warning("could not read due date %r", value)
        return None


def format_date(due_at: datetime | None) -> str:
    return "" if due_at is None else f"{tu.to_local(due_at):%Y-%m-%d}"


def parse_percent(value: str | None) -> float:
    if value is None or str(value).strip() == "":
        return 0.0
    text = str(value).strip().rstrip("%").strip()
    try:
        number = float(text)
    except ValueError:
        log.warning("could not read %% done %r", value)
        return 0.0
    if 0 < number <= 1 and "." in text:
        number *= 100  # someone typed 0.45
    return max(0.0, min(number, 100.0))


def format_percent(percent: float) -> str:
    return str(int(round(percent or 0)))


@dataclass(slots=True)
class SheetRow:
    row_number: int                      # 1-based, as the API sees it
    values: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str:
        return (self.values.get(key) or "").strip()


class SheetsClient:
    def __init__(self, service=None) -> None:
        self._service = service

    @property
    def service(self):
        if self._service is None:
            self._service = google_auth.build_service("sheets", "v4")
        return self._service

    # --- creation ------------------------------------------------------
    def create(self, title: str = config.SPREADSHEET_TITLE) -> str:
        body = {
            "properties": {"title": title},
            "sheets": [
                {
                    "properties": {
                        "title": SHEET_TITLE,
                        "gridProperties": {"frozenRowCount": 1},
                    }
                }
            ],
        }
        created = google_auth.execute(self.service.spreadsheets().create(body=body))
        spreadsheet_id = created["spreadsheetId"]
        self.write_values(spreadsheet_id, "A1", [HEADERS])
        self._format_header(spreadsheet_id, created["sheets"][0]["properties"]["sheetId"])
        return spreadsheet_id

    def _format_header(self, spreadsheet_id: str, sheet_id: int) -> None:
        requests = [
            {
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "endRowIndex": 1},
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"bold": True},
                            "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
                        }
                    },
                    "fields": "userEnteredFormat(textFormat,backgroundColor)",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": 1,
                    },
                    "properties": {"pixelSize": 260},
                    "fields": "pixelSize",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": len(COLUMNS) - 1,
                        "endIndex": len(COLUMNS),
                    },
                    "properties": {"pixelSize": 70},
                    "fields": "pixelSize",
                }
            },
        ]
        google_auth.execute(
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests}
            )
        )

    def url(self, spreadsheet_id: str) -> str:
        return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"

    # --- reading -------------------------------------------------------
    def fetch_grid(self, spreadsheet_id: str) -> list[list[str]]:
        response = google_auth.execute(
            self.service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=SHEET_TITLE)
        )
        return response.get("values", [])

    def read(self, spreadsheet_id: str) -> tuple[dict[str, int], list[SheetRow]]:
        """(canonical name -> column index, rows). Header order is whatever it is."""
        grid = self.fetch_grid(spreadsheet_id)
        if not grid:
            return {}, []

        header = grid[0]
        index: dict[str, int] = {}
        for position, cell in enumerate(header):
            key = _normalise(cell)
            for name, _, aliases in COLUMNS:
                if key == _normalise(name) or key in aliases:
                    index.setdefault(name, position)
                    break

        rows = []
        for offset, raw in enumerate(grid[1:], start=2):
            values = {
                name: (raw[pos] if pos < len(raw) else "") for name, pos in index.items()
            }
            if not any(v.strip() for v in values.values()):
                continue  # blank spacer row
            rows.append(SheetRow(row_number=offset, values=values))
        return index, rows

    # --- writing -------------------------------------------------------
    def write_values(self, spreadsheet_id: str, anchor: str, values: list[list]) -> None:
        google_auth.execute(
            self.service.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=f"{SHEET_TITLE}!{anchor}",
                valueInputOption="USER_ENTERED",
                body={"values": values},
            )
        )

    def update_cells(self, spreadsheet_id: str, updates: list[tuple[str, object]]) -> None:
        """[(A1 range, value)] in one round trip. Only the cells that changed."""
        if not updates:
            return
        data = [
            {"range": f"{SHEET_TITLE}!{cell}", "values": [[value]]}
            for cell, value in updates
        ]
        google_auth.execute(
            self.service.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            )
        )


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters
