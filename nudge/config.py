"""All tuning knobs in one file. Nothing else hardcodes a number that belongs here."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env reader so secrets stay out of the shell profile."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()

# --- when to nag ---------------------------------------------------------
TICK_INTERVAL = 10              # minutes between engine ticks (each one reads the sheet)
MIN_GAP_MINUTES = 15            # shorter free time is not worth a message
MIN_SESSION_MINUTES = 10        # never carve off a slice shorter than this
TAIL_BUFFER = 5                 # minutes kept free before your next event
MAX_SESSION_MINUTES = 120       # a free afternoon is not a five-hour sitting
GLOBAL_COOLDOWN = 45            # minutes between any two offers
MAX_OFFERS_PER_DAY = 6
QUIET_HOURS = (23, 8)           # local hours [start, end); wraps midnight
MAX_DECLINES = 4                # in a row, then it is set aside until tomorrow
DEFAULT_BUSY_HOURS = 3          # "busy" with no readable duration
TIMEZONE = os.environ.get("NUDGE_TIMEZONE", "America/New_York")

# --- how much time there is ----------------------------------------------
# How much of a day realistically goes to coursework. Real free time from the
# calendar can only lower this; CAPACITY_REALISM discounts it further, since
# free time is not the same as time you will actually spend.
WORKABLE_HOURS_PER_DAY = 4.0
CAPACITY_REALISM = 0.6
DEFAULT_EXPECTED_MINUTES = 120  # when the sheet's Expected length cell is blank
CAPACITY_HORIZON_DAYS = 60      # how far ahead the daily check looks

# --- google --------------------------------------------------------------
CREDENTIALS_PATH = Path(
    os.environ.get("NUDGE_GOOGLE_CREDENTIALS", PROJECT_ROOT / "credentials.json")
)
TOKEN_PATH = Path(os.environ.get("NUDGE_GOOGLE_TOKEN", PROJECT_ROOT / "token.json"))
SPREADSHEET_TITLE = "nudge — assignments"
# None means every calendar in the account. Subscribed read-only calendars
# (holidays, sports schedules) often 404 on freeBusy — they are skipped
# automatically after the first failure, but naming them here avoids the round
# trip entirely.
CALENDARS: tuple[str, ...] | None = None
CALENDAR_EXCLUDE: tuple[str, ...] = ("Holidays in United States",)
FREEBUSY_WINDOW_HOURS = 12      # how far ahead each tick looks

# --- sizing assignments from their documents ------------------------------
ESTIMATE_MODEL = "claude-opus-5-5"
MIN_EXPECTED_MINUTES = 15
MAX_EXPECTED_MINUTES = 60 * 40  # a 40-hour assignment is a semester project

# --- runtime -------------------------------------------------------------
DB_PATH = Path(os.environ.get("NUDGE_DB", PROJECT_ROOT / "nudge.db"))
LOG_LEVEL = os.environ.get("NUDGE_LOG_LEVEL", "INFO")
DISCORD_TOKEN = os.environ.get("NUDGE_DISCORD_TOKEN", "")
DISCORD_USER_ID = int(os.environ.get("NUDGE_DISCORD_USER_ID") or 0)

TZ = ZoneInfo(TIMEZONE)
