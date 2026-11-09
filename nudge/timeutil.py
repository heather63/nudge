"""Timezone-aware helpers. Store UTC, compare in local (plan §12)."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

from . import config

UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(config.TZ)


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat()


def parse(value: str | None) -> datetime | None:
    """Parse a stored ISO8601 string. Naive input is treated as UTC."""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def local_date(dt: datetime | None = None) -> date:
    return to_local(dt or now_utc()).date()


def minutes_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


def fmt_duration(minutes: float) -> str:
    m = int(round(minutes))
    if m < 60:
        return f"{m} min"
    hours, rem = divmod(m, 60)
    if rem == 0:
        return f"{hours}h"
    return f"{hours}h{rem:02d}"


def fmt_due(dt: datetime | None, *, now: datetime | None = None) -> str:
    if dt is None:
        return "no due date"
    now = now or now_utc()
    local = to_local(dt)
    days = (local.date() - local_date(now)).days
    if days == 0:
        return "due today"
    if days == 1:
        return "due tomorrow"
    if days < 0:
        return f"overdue by {abs(days)}d"
    if days <= 6:
        return f"due {local:%A}"
    return f"due {local:%b %-d}"


WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_REL = re.compile(r"^\s*(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\s*$", re.I)
_CLOCK = re.compile(r"^\s*(?:until\s+|till\s+|til\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", re.I)
_WEEKDAY = re.compile(
    r"\b(?P<day>mon|monday|tue|tues|tuesday|wed|weds|wednesday|thu|thur|thurs|"
    r"thursday|fri|friday|sat|saturday|sun|sunday)\b",
    re.I,
)


def _local_at(moment: datetime, at: time) -> datetime:
    """`at` o'clock on the local date of `moment`."""
    return datetime.combine(to_local(moment).date(), at, config.TZ).astimezone(UTC)


def parse_when(text: str, *, now: datetime | None = None) -> datetime | None:
    """Parse a snooze/mute duration into an absolute UTC instant.

    Accepts '90m', '2h', '3 days', 'tomorrow', 'tonight', 'until 3pm'.
    Returns None when nothing recognisable is found.
    """
    now = now or now_utc()
    text = (text or "").strip().lower()
    if not text:
        return None

    text = re.sub(r"^(?:until|till|til|thru|through|for|after)\s+", "", text).strip()
    if not text:
        return None

    if text in {"tomorrow", "tmrw", "morning"}:
        return _local_at(now + timedelta(days=1), time(config.QUIET_HOURS[1], 0))

    if text in {"tonight", "evening"}:
        local = to_local(now)
        target = datetime.combine(local.date(), time(20, 0), config.TZ)
        if target <= local:
            target += timedelta(days=1)
        return target.astimezone(UTC)

    m = _REL.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0]
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        return now + delta

    if text in {"rest of the day", "today", "eod", "end of day", "the day"}:
        return _local_at(now + timedelta(days=1), time(config.QUIET_HOURS[1], 0))

    m = _WEEKDAY.search(text)
    if m:
        target = WEEKDAYS[m.group("day").lower()]
        clock = _CLOCK.match(text[m.end():].strip()) if text[m.end():].strip() else None
        at = time(config.QUIET_HOURS[1], 0)
        if clock:
            hour, minute = int(clock.group(1)), int(clock.group(2) or 0)
            meridiem = clock.group(3)
            if meridiem == "pm" and hour < 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
            if hour <= 23 and minute <= 59:
                at = time(hour, minute)
        local = to_local(now)
        for offset in range(0, 8):
            candidate = local + timedelta(days=offset)
            if candidate.weekday() != target:
                continue
            resolved = _local_at(candidate, at)
            if resolved > now:
                return resolved
        return None

    m = _CLOCK.match(text)
    if m:
        hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None
        local = to_local(now)
        target = datetime.combine(local.date(), time(hour, minute), config.TZ)
        if target <= local:
            target += timedelta(days=1)
        return target.astimezone(UTC)

    return None
