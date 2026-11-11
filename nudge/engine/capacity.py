"""How much work is left, against how much time there is to do it.

Two consumers on the same math, so the nag and the warning can never disagree:
the `pressure` term in scoring (every tick, per assignment) and the once-daily
"this no longer fits" check.

Availability comes from real free intervals when they have been measured, and
falls back to WORKABLE_HOURS_PER_DAY when they have not.
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .. import config
from .. import timeutil as tu
from ..store.repo import Repo

if TYPE_CHECKING:
    from ..sheet import Assignment

log = logging.getLogger(__name__)

Interval = tuple[datetime, datetime]

CAPACITY_SNAPSHOT = "capacity_snapshot"
LAST_CAPACITY_CHECK = "last_capacity_check"
SNAPSHOT_TTL_HOURS = 30.0  # a daily check that slipped a few hours is still good


# --- quiet hours ---------------------------------------------------------
def waking_windows(day: datetime) -> list[Interval]:
    """The non-quiet stretches of one local day, as aware datetimes."""
    start_hour, end_hour = config.QUIET_HOURS
    midnight = day.replace(hour=0, minute=0, second=0, microsecond=0)
    next_midnight = midnight + timedelta(days=1)

    def at(hour: int) -> datetime:
        return midnight + timedelta(hours=hour)

    if start_hour > end_hour:  # quiet wraps midnight, e.g. 23:00-08:00
        return [(at(end_hour), at(start_hour))]
    # quiet sits inside the day, e.g. 02:00-06:00
    return [(midnight, at(start_hour)), (at(end_hour), next_midnight)]


def waking_minutes(start: datetime, end: datetime) -> float:
    """Minutes between two instants, excluding quiet hours."""
    if end <= start:
        return 0.0
    total = 0.0
    cursor = tu.to_local(start)
    stop = tu.to_local(end)
    while cursor < stop:
        day_end = min(
            stop,
            (cursor + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ),
        )
        for window_start, window_end in waking_windows(cursor):
            low = max(cursor, window_start)
            high = min(day_end, window_end)
            if high > low:
                total += (high - low).total_seconds() / 60.0
        cursor = day_end
    return total


# --- availability --------------------------------------------------------
def available_minutes(
    now: datetime,
    until: datetime | None,
    free_intervals: list[Interval] | None = None,
    opens_at: datetime | None = None,
) -> float:
    """Minutes plausibly available for work between now and a due date.

    `opens_at` moves the start of the window: a task that cannot be begun until
    Friday morning does not have all week to be done in.
    """
    if until is None:
        return math.inf
    if opens_at is not None and opens_at > now:
        now = opens_at
    if until <= now:
        return 0.0
    if free_intervals is None:
        # No measured calendar data — assume a flat workable slice of each day.
        days = tu.minutes_between(now, until) / 1440.0
        return days * config.WORKABLE_HOURS_PER_DAY * 60.0

    # The calendar says when you are free; it does not say you will work all of
    # it. An empty day reads as ~15 waking hours, which would make every
    # assignment look comfortable forever. So bucket free time by local day and
    # cap each day at what you would actually spend. The calendar can only make
    # a day *worse* than that cap, never better.
    per_day: dict[object, float] = defaultdict(float)
    for start, end in free_intervals:
        window_start, window_end = max(start, now), min(end, until)
        if window_end <= window_start:
            continue
        cursor = tu.to_local(window_start)
        stop = tu.to_local(window_end)
        while cursor < stop:
            day_end = min(
                stop,
                (cursor + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ),
            )
            per_day[cursor.date()] += waking_minutes(cursor, day_end)
            cursor = day_end

    daily_cap = config.WORKABLE_HOURS_PER_DAY * 60.0
    return sum(min(minutes, daily_cap) for minutes in per_day.values())


def usable_minutes(
    now: datetime,
    until: datetime | None,
    free_intervals: list[Interval] | None = None,
    available_min: float | None = None,
) -> float:
    """Available time discounted by the realism factor."""
    available = (
        available_min
        if available_min is not None
        else available_minutes(now, until, free_intervals)
    )
    if available == math.inf:
        return math.inf
    return available * config.CAPACITY_REALISM


def pressure(
    remaining_min: float,
    now: datetime,
    due_at: datetime | None,
    available_min: float | None = None,
) -> float:
    """Remaining work over realistic time left. Above 1.0 it no longer fits.

    This is what makes a ten-hour assignment due Friday outrank a one-hour
    assignment due Wednesday: size counts, not just proximity.
    """
    if remaining_min <= 0:
        return 0.0
    usable = usable_minutes(now, due_at, available_min=available_min)
    if usable == math.inf:
        return 0.0  # no due date, no deadline pressure
    if usable <= 0:
        return math.inf  # overdue, or due within the hour
    return remaining_min / usable


def headroom_minutes(
    remaining_min: float,
    now: datetime,
    due_at: datetime | None,
    available_min: float | None = None,
) -> float:
    """Slack: usable time minus remaining work. Negative means it does not fit."""
    usable = usable_minutes(now, due_at, available_min=available_min)
    if usable == math.inf:
        return math.inf
    return usable - remaining_min


# --- the daily check -----------------------------------------------------
@dataclass(slots=True)
class CapacityWarning:
    sheet_id: str
    name: str
    remaining_min: float
    usable_min: float
    due_at: datetime | None

    @property
    def short_by(self) -> float:
        return self.remaining_min - self.usable_min


def read_snapshot(repo: Repo, now: datetime) -> dict[str, float]:
    """Measured availability per assignment, while it is still fresh."""
    raw = repo.kv_get(CAPACITY_SNAPSHOT)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    measured_at = tu.parse(payload.get("measured_at"))
    if measured_at is None:
        return {}
    if tu.minutes_between(measured_at, now) / 60.0 > SNAPSHOT_TTL_HOURS:
        return {}
    return {str(k): float(v) for k, v in payload.get("available", {}).items()}


def write_snapshot(repo: Repo, now: datetime, available: dict[str, float]) -> None:
    repo.kv_set(
        CAPACITY_SNAPSHOT,
        json.dumps({"measured_at": tu.iso(now), "available": available}),
    )


def due_today(repo: Repo, now: datetime) -> bool:
    last = repo.kv_get(LAST_CAPACITY_CHECK)
    return last != tu.local_date(now).isoformat()


def daily_check(
    assignments: list[Assignment], repo: Repo, now: datetime, free_intervals_fn=None
) -> list[CapacityWarning]:
    """Compare remaining work against real free time. Warn once per assignment.

    A scalar comparison, not a scheduler — but it is the only part of the system
    that says something you did not already know.
    """
    warned = set(json.loads(repo.kv_get("capacity_warned") or "[]"))
    still_warned: set[str] = set()
    measured: dict[str, float] = {}
    warnings: list[CapacityWarning] = []

    rows = [a for a in assignments if a.due_at is not None and a.remaining_min > 0]

    # One freeBusy call for the furthest due date, sliced per assignment.
    intervals = None
    if free_intervals_fn is not None and rows:
        horizon = min(
            max(a.due_at for a in rows),
            now + timedelta(days=config.CAPACITY_HORIZON_DAYS),
        )
        if horizon > now:
            try:
                intervals = free_intervals_fn(now, horizon)
            except Exception:  # noqa: BLE001 - fall back to the flat estimate
                log.exception("freeBusy failed for the capacity check")

    for a in rows:
        available = available_minutes(now, a.due_at, intervals, a.available_from)
        if available != math.inf:
            measured[a.sheet_id] = available
        usable = usable_minutes(now, a.due_at, available_min=available)
        if a.remaining_min > usable:
            still_warned.add(a.sheet_id)
            if a.sheet_id not in warned:
                warnings.append(
                    CapacityWarning(a.sheet_id, a.label, a.remaining_min, usable, a.due_at)
                )

    if measured:
        write_snapshot(repo, now, measured)
    # An assignment that fits again is eligible to warn a second time later.
    repo.kv_set("capacity_warned", json.dumps(sorted(still_warned)))
    repo.kv_set(LAST_CAPACITY_CHECK, tu.local_date(now).isoformat())
    return warnings
