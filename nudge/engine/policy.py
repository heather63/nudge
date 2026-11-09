"""Quiet hours, budgets, cooldowns, escalation (plan §5, §7)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import config
from .. import timeutil as tu
from ..store.repo import Repo
from .gaps import Gap

# kv keys
MUTE_UNTIL = "mute_until"
LAST_OFFER_AT = "last_offer_at"
NO_OFFER_UNTIL = "no_offer_until"  # set by "not now" — silences the rest of a gap


@dataclass(slots=True)
class Verdict:
    ok: bool
    reason: str = ""


def in_quiet_hours(now: datetime) -> bool:
    start, end = config.QUIET_HOURS
    hour = tu.to_local(now).hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def quiet_hours_end(now: datetime) -> datetime:
    """Next local instant quiet hours lift. Used for 'snooze until morning'."""
    _, end = config.QUIET_HOURS
    local = tu.to_local(now)
    target = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(tu.UTC)


def gap_is_usable(gap: Gap, now: datetime) -> Verdict:
    if gap.length_min - config.TAIL_BUFFER < config.MIN_GAP_MINUTES:
        return Verdict(False, "gap too short")
    if now < gap.start + timedelta(minutes=config.LEAD_IN_DELAY):
        return Verdict(False, "lead-in delay")
    if gap.usable_min(now) < config.MIN_GAP_MINUTES:
        return Verdict(False, "not enough of the gap left")
    return Verdict(True)


def may_nag(repo: Repo, now: datetime) -> Verdict:
    """Global gates that have nothing to do with any particular gap."""
    if in_quiet_hours(now):
        return Verdict(False, "quiet hours")

    mute_until = repo.kv_time(MUTE_UNTIL)
    if mute_until and mute_until > now:
        return Verdict(False, f"muted until {tu.to_local(mute_until):%-I:%M%p}".lower())

    no_offer_until = repo.kv_time(NO_OFFER_UNTIL)
    if no_offer_until and no_offer_until > now:
        return Verdict(False, "declined this gap")

    if repo.offers_today() >= config.MAX_OFFERS_PER_DAY:
        return Verdict(False, "daily offer budget spent")

    last_offer = repo.kv_time(LAST_OFFER_AT)
    if last_offer and tu.minutes_between(last_offer, now) < config.GLOBAL_COOLDOWN:
        return Verdict(False, "global cooldown")

    return Verdict(True)


def escalation_level(decline_count: int) -> int:
    """1 neutral, 2-3 direct, 4-5 blunt, 6 final (plan §7).

    Keyed on *consecutive declines*, not offers: a twenty-hour project is
    legitimately offered many times, and any progress resets the count.
    """
    return min(max(decline_count + 1, 1), config.MAX_ESCALATION)


def is_final_offer(decline_count: int) -> bool:
    return decline_count + 1 >= config.MAX_ESCALATION
