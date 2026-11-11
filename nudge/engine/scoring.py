"""Ranking the chunk pile. Plain weighted sum, deterministic."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .. import config
from .. import timeutil as tu
from ..store.repo import Candidate
from . import capacity


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def urgency(due_at: datetime | None, now: datetime) -> float:
    """Superlinear in due-date proximity, so tomorrow's work outranks easy wins."""
    if due_at is None:
        return config.NULL_DUE_URGENCY
    hours_left = max(tu.minutes_between(now, due_at) / 60.0, 1.0)
    return clamp(((72.0 / hours_left) ** 1.5) / 100.0)


def fit(remaining_min: float, usable_gap_min: float) -> float:
    """How well this assignment suits the gap in front of you.

    Work is time-boxed to the gap now rather than pre-cut into chunks, so
    nothing is ever disqualified — you can always make *some* progress. What
    fit still rewards is being able to close something out in one sitting.
    """
    if usable_gap_min <= 0:
        return 0.0
    if remaining_min <= usable_gap_min:
        return 1.0
    return 0.4 + 0.6 * (usable_gap_min / remaining_min)


def session_minutes(remaining_min: float, usable_gap_min: float) -> int:
    """How long to actually ask for: the gap, or what is left, whichever is less."""
    return int(max(0.0, min(remaining_min, usable_gap_min)))


def staleness(last_offered_at: datetime | None, created_at: datetime, now: datetime) -> float:
    reference = last_offered_at or created_at
    days = tu.minutes_between(reference, now) / 1440.0
    return clamp(days / config.STALENESS_FULL_DAYS)


def decline_penalty(decline_count: int, last_decline_at: datetime | None, now: datetime) -> float:
    if not decline_count or last_decline_at is None:
        return float(decline_count)
    hours = max(tu.minutes_between(last_decline_at, now) / 60.0, 0.0)
    return decline_count * math.exp(-hours / config.DECLINE_HALFLIFE_HOURS)


@dataclass(slots=True)
class Scored:
    candidate: Candidate
    score: float
    parts: dict[str, float] = field(default_factory=dict)


def score_candidate(
    candidate: Candidate,
    *,
    now: datetime,
    usable_gap_min: float,
    continuity_assignment_id: int | None,
    last_decline_at: datetime | None,
    available_min: float | None = None,
) -> Scored:
    parts = {
        "urgency": urgency(candidate.due_at, now),
        "pressure": capacity.pressure(
            candidate.remaining_min, now, candidate.due_at, available_min
        ),
        "fit": fit(candidate.remaining_min, usable_gap_min),
        "staleness": staleness(candidate.last_offered_at, candidate.created_at, now),
        "continuity": 1.0 if candidate.assignment_id == continuity_assignment_id else 0.0,
        "decline": decline_penalty(candidate.decline_count, last_decline_at, now),
    }
    total = (
        config.W_URGENCY * parts["urgency"]
        + config.W_PRESSURE * parts["pressure"]
        + config.W_FIT * parts["fit"]
        + config.W_STALENESS * parts["staleness"]
        + config.W_CONTINUITY * parts["continuity"]
        - config.W_DECLINE * parts["decline"]
    )
    return Scored(candidate=candidate, score=total, parts=parts)


def rank(
    candidates: list[Candidate],
    *,
    now: datetime,
    usable_gap_min: float,
    continuity_assignment_id: int | None,
    last_decline_lookup,
    exclude: set[int] | None = None,
    availability: dict[int, float] | None = None,
) -> list[Scored]:
    """Rank eligible chunks best-first. Chunks that cannot fit are dropped."""
    exclude = exclude or set()
    eligible = []
    for candidate in candidates:
        if candidate.assignment_id in exclude:
            continue
        if candidate.snooze_until and candidate.snooze_until > now:
            continue
        if candidate.available_from and candidate.available_from > now:
            continue  # can't be started yet, whatever the deadline says
        if candidate.remaining_min <= 0:
            continue
        # MIN_SESSION_MINUTES exists to stop a pointless slice being carved off
        # a large job in a short gap. It must never block *finishing* something:
        # an assignment with four minutes left would otherwise be unofferable
        # for ever and could never reach 100%.
        if (
            candidate.remaining_min > usable_gap_min
            and usable_gap_min < config.MIN_SESSION_MINUTES
        ):
            continue
        eligible.append(candidate)

    scored = [
        score_candidate(
            c,
            now=now,
            usable_gap_min=usable_gap_min,
            continuity_assignment_id=continuity_assignment_id,
            last_decline_at=last_decline_lookup(c.assignment_id),
            available_min=(availability or {}).get(c.assignment_id),
        )
        for c in eligible
    ]
    scored.sort(key=lambda s: (-s.score, s.candidate.assignment_id))
    return scored
