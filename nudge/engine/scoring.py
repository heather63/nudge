"""Which assignment to offer: the one under the most pressure.

pressure = remaining work / realistic free time before it's due

That single number covers both urgency and size: a ten-hour project due Friday
outranks a one-hour reading due Wednesday. Ties go to whatever you could
finish in this sitting.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .. import config
from ..sheet import Assignment
from ..store.repo import State
from . import capacity


def fit(remaining_min: float, usable_gap_min: float) -> float:
    """1.0 if this sitting could finish it, falling off as it gets bigger."""
    if usable_gap_min <= 0:
        return 0.0
    if remaining_min <= usable_gap_min:
        return 1.0
    return usable_gap_min / remaining_min


def session_minutes(remaining_min: float, usable_gap_min: float) -> int:
    """How long to ask for: the gap, or what is left, whichever is less."""
    return int(max(0.0, min(remaining_min, usable_gap_min)))


@dataclass(slots=True)
class Scored:
    assignment: Assignment
    pressure: float
    fit: float


def rank(
    assignments: list[Assignment],
    *,
    now: datetime,
    usable_gap_min: float,
    states: dict[str, State] | None = None,
    exclude: set[str] | None = None,
    availability: dict[str, float] | None = None,
) -> list[Scored]:
    """Everything worth offering in this gap, best first."""
    states = states or {}
    exclude = exclude or set()
    availability = availability or {}
    scored = []
    for a in assignments:
        state = states.get(a.sheet_id)
        if a.done or a.remaining_min <= 0 or a.sheet_id in exclude:
            continue
        if state and state.snooze_until and state.snooze_until > now:
            continue
        if a.available_from and a.available_from > now:
            continue  # can't be started yet, whatever the deadline says
        # Don't carve a pointless slice off a big job in a short gap — but never
        # block *finishing* something, or it could never reach 100%.
        if a.remaining_min > usable_gap_min and usable_gap_min < config.MIN_SESSION_MINUTES:
            continue
        scored.append(Scored(
            assignment=a,
            pressure=capacity.pressure(
                a.remaining_min, now, a.due_at, availability.get(a.sheet_id)
            ),
            fit=fit(a.remaining_min, usable_gap_min),
        ))
    scored.sort(key=lambda s: (
        -s.pressure,
        -s.fit,
        s.assignment.due_at or datetime.max.replace(tzinfo=now.tzinfo),
        s.assignment.name,
    ))
    return scored
