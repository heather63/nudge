"""All tuning knobs in one file (plan §10).

These get changed constantly in the first month. Nothing else in the codebase
should hardcode a number that belongs here.
"""
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

# --- timing -------------------------------------------------------------
MIN_GAP_MINUTES = 15
MIN_SESSION_MINUTES = 10        # not worth offering a shorter sitting than this
TAIL_BUFFER = 5       # minutes shaved off the end of every gap
LEAD_IN_DELAY = 4     # minutes to wait after a gap opens before firing
TICK_INTERVAL = 10    # minutes between engine ticks
GLOBAL_COOLDOWN = 45  # minutes between any two offers
MAX_OFFERS_PER_DAY = 6
QUIET_HOURS = (23, 8)  # local hours [start, end); wraps midnight
MAX_ESCALATION = 6
DEFAULT_BUSY_HOURS = 3          # "busy" with no readable duration
TIMEZONE = "America/New_York"

# --- scoring weights (plan §6) ------------------------------------------
# Retune these against the offers table after a couple of weeks. W_FIT is lower
# than the original plan's 0.30 because `fit` discriminates less once work is
# time-boxed to the gap rather than pre-cut into chunks.
W_URGENCY = 0.40
W_PRESSURE = 0.35
W_FIT = 0.20
W_STALENESS = 0.10
W_CONTINUITY = 0.10
W_DECLINE = 0.30

# How much of a day realistically goes to coursework at all. Phase 3 replaces
# this constant with real free time from freeBusy; CAPACITY_REALISM stays, since
# free time is not the same as time you will actually spend (plan §9).
WORKABLE_HOURS_PER_DAY = 4.0
CAPACITY_REALISM = 0.6

CONTINUITY_WINDOW_HOURS = 24
DECLINE_HALFLIFE_HOURS = 6.0
NULL_DUE_URGENCY = 0.15
STALENESS_FULL_DAYS = 7.0

# --- fake gaps -----------------------------------------------------------
# The phase-1 stand-in for freeBusy. Turn it on to exercise the nag policy
# without a calendar; nudge also falls back to it when Google is unauthorised.
FAKE_GAPS = False
FAKE_GAP_EVERY_MIN = 90            # a synthetic gap opens this often
FAKE_GAP_LENGTHS = (20, 30, 45, 60)  # cycled in order

# --- google (phase 2-3) --------------------------------------------------
CREDENTIALS_PATH = Path(
    os.environ.get("NUDGE_GOOGLE_CREDENTIALS", PROJECT_ROOT / "credentials.json")
)
TOKEN_PATH = Path(os.environ.get("NUDGE_GOOGLE_TOKEN", PROJECT_ROOT / "token.json"))
# One Google task list per course. None means every list in the account;
# name titles in TASKLIST_EXCLUDE to leave some alone.
TASKLISTS: tuple[str, ...] | None = None
TASKLIST_EXCLUDE: tuple[str, ...] = ()
DEFAULT_TASKLIST = "naggy"  # home for work with no course. Matches the existing Google Tasks list by title; rename that list in Google Tasks before changing this.
TASKS_SYNC_MINUTES = 5          # Tasks has no push channel, so poll (plan §4.4)
# A task typed straight into the Tasks app becomes a sheet row on the next
# reconcile. The sheet stays truth for existence — this is how something gets
# INTO it from the other side, not a second authority over what already exists.
ADOPT_NEW_TASKS = True
SYNC_LOOKBACK_MINUTES = 2       # cursor overlap, to survive clock skew
DEFAULT_EXPECTED_MINUTES = 120  # when the sheet's expected-length cell is blank
SPREADSHEET_TITLE = "nudge — assignments"
SHEET_SYNC_MINUTES = 5          # the sheet has no push channel either

# --- calendar / gap detection (phase 3) ----------------------------------
# None means every calendar in the account. Subscribed read-only calendars
# (holidays, sports schedules) often 404 on freeBusy — they are skipped
# automatically after the first failure, but naming them here avoids the round
# trip entirely.
CALENDARS: tuple[str, ...] | None = None
CALENDAR_EXCLUDE: tuple[str, ...] = ("Holidays in United States",)
FREEBUSY_WINDOW_HOURS = 12      # how far ahead each tick looks
# Look back as well as forward, so a free interval keeps its REAL start time.
# Inverting busy intervals from `now` would make every current gap appear to
# have just opened, and LEAD_IN_DELAY would block every offer forever.
FREEBUSY_LOOKBACK_MINUTES = 90
FREEBUSY_CACHE_SECONDS = 120    # don't re-query on every command
CAPACITY_HORIZON_DAYS = 60      # freeBusy range cap for the daily check

# A genuinely free afternoon is not a five-hour work session. Capping the
# usable gap keeps `fit` discriminating — without it everything scores 1.0 in a
# long gap and ranking collapses onto urgency alone. It is also the longest
# single sitting nudge will ever ask for.
MAX_USABLE_GAP_MINUTES = 120

# --- ingestion + decomposition (phase 4) ---------------------------------
# One folder per course, one subfolder per assignment:
#   ~/assignments/CS 4262/HW1/spec.pdf
# The course folder matches a Google task list, the assignment folder matches a
# task in it. Folder identity beats guessing which files belong together.
WATCH_FOLDER = "~/assignments"
WATCH_ENABLED = True
DEBOUNCE_SECONDS = 5.0          # editors emit a burst of events per save
ESTIMATE_MODEL = "claude-opus-5"
MIN_EXPECTED_MINUTES = 15
MAX_EXPECTED_MINUTES = 60 * 40  # a 40-hour assignment is a semester project

# --- runtime -------------------------------------------------------------
DB_PATH = Path(os.environ.get("NUDGE_DB", PROJECT_ROOT / "nudge.db"))
SEED_FILE = PROJECT_ROOT / "chunks.seed.json"
LOG_LEVEL = os.environ.get("NUDGE_LOG_LEVEL", "INFO")

DISCORD_TOKEN = os.environ.get("NUDGE_DISCORD_TOKEN", "")
DISCORD_USER_ID = int(os.environ.get("NUDGE_DISCORD_USER_ID") or 0)

TZ = ZoneInfo(TIMEZONE)
