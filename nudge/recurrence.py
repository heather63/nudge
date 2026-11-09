"""Repeating assignments.

A weekly quiz is one row in the sheet, not fifteen. The row carries a rule in
the Repeats column and its due date rolls forward to the next occurrence once
the current one is done or has passed.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta

from . import config
from . import timeutil as tu

WEEKDAYS = tu.WEEKDAYS
NAMES = tu.WEEKDAY_NAMES


def parse_rule(text: str | None) -> set[int] | None:
    """'weekly:fri' / 'fri' / 'sun,tue' / 'Mondays' -> {weekday numbers}."""
    if not text:
        return None
    cleaned = re.sub(r"^\s*(every|weekly|each)\s*[:\s]\s*", "", str(text).strip(), flags=re.I)
    days: set[int] = set()
    for token in re.split(r"[,\s/&]+", cleaned):
        key = token.strip().lower().rstrip("s") if token.strip().lower() not in WEEKDAYS else token.strip().lower()
        if key in WEEKDAYS:
            days.add(WEEKDAYS[key])
        elif token.strip().lower() in WEEKDAYS:
            days.add(WEEKDAYS[token.strip().lower()])
    return days or None


def format_rule(days: set[int] | None) -> str:
    if not days:
        return ""
    return "weekly:" + ",".join(NAMES[d] for d in sorted(days))


def next_due(days: set[int], after: datetime, hour: int = 23, minute: int = 59) -> datetime:
    """The next matching weekday strictly after `after`, at end of the local day."""
    local = tu.to_local(after)
    for offset in range(1, 15):
        candidate = local + timedelta(days=offset)
        if candidate.weekday() in days:
            return datetime.combine(
                candidate.date(), time(hour, minute), config.TZ
            ).astimezone(tu.UTC)
    raise ValueError("no matching weekday")


def due_now(days: set[int], now: datetime, hour: int = 23, minute: int = 59) -> datetime:
    """Today if it matches and is still ahead, otherwise the next occurrence."""
    local = tu.to_local(now)
    if local.weekday() in days:
        today = datetime.combine(
            local.date(), time(hour, minute), config.TZ
        ).astimezone(tu.UTC)
        if today > now:
            return today
    return next_due(days, now, hour, minute)


# --- "not before": the earliest an occurrence can actually be started --------
_TIME = re.compile(r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<mer>am|pm)?\b", re.I)


class NotBefore:
    """When work on an occurrence can begin, relative to its due date.

    'fri 10am' means the Friday at or before the due date, at 10:00 — so a
    weekly task due Friday 23:59 opens that morning, and a quiz due Monday
    23:59 that is released Friday 5pm opens the Friday before.
    """

    __slots__ = ("days", "at", "absolute")

    def __init__(self, days=None, at=None, absolute=None):
        self.days = days
        self.at = at
        self.absolute = absolute

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"NotBefore(days={self.days}, at={self.at}, absolute={self.absolute})"


def parse_not_before(text: str | None) -> NotBefore | None:
    """'fri 10am' / '10am' / '2026-10-01' -> a rule, or None if unreadable."""
    if not text or not str(text).strip():
        return None
    raw = str(text).strip()

    days = parse_rule(re.sub(_TIME, " ", raw))
    match = _TIME.search(raw)
    if match:
        hour = int(match.group("h"))
        minute = int(match.group("m") or 0)
        meridiem = (match.group("mer") or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if hour <= 23 and minute <= 59:
            return NotBefore(days=days, at=time(hour, minute))
    if days:
        return NotBefore(days=days, at=time(0, 0))

    from .connectors import gsheets  # local import: leaf module stays light

    absolute = gsheets.parse_date(raw)
    if absolute is not None:
        local = tu.to_local(absolute)
        return NotBefore(
            absolute=datetime.combine(local.date(), time(0, 0), config.TZ).astimezone(tu.UTC)
        )
    return None


def resolve_not_before(rule: NotBefore | None, due_at: datetime | None) -> datetime | None:
    """The latest moment matching the rule that is at or before the due date."""
    if rule is None:
        return None
    if rule.absolute is not None:
        return rule.absolute
    if due_at is None:
        return None

    local_due = tu.to_local(due_at)
    for offset in range(0, 8):
        day = local_due.date() - timedelta(days=offset)
        if rule.days and day.weekday() not in rule.days:
            continue
        candidate = datetime.combine(day, rule.at, config.TZ).astimezone(tu.UTC)
        if candidate <= due_at:
            return candidate
    return None
