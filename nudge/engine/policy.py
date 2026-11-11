"""Whether nudge may message you right now."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .. import config
from .. import timeutil as tu
from ..store.repo import Repo
from .gaps import Gap

# kv keys
BUSY_UNTIL = "busy_until"
NO_OFFER_UNTIL = "no_offer_until"  # set by "not now": silences the rest of a gap


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


def may_nag(repo: Repo, gap: Gap, now: datetime) -> Verdict:
    if gap.usable_min(now) < config.MIN_GAP_MINUTES:
        return Verdict(False, "gap too short")
    if in_quiet_hours(now):
        return Verdict(False, "quiet hours")
    busy_until = repo.kv_time(BUSY_UNTIL)
    if busy_until and busy_until > now:
        return Verdict(False, f"busy until {tu.fmt_when(busy_until)}")
    no_offer_until = repo.kv_time(NO_OFFER_UNTIL)
    if no_offer_until and no_offer_until > now:
        return Verdict(False, "declined this gap")
    if repo.offers_today(now) >= config.MAX_OFFERS_PER_DAY:
        return Verdict(False, "daily offer budget spent")
    last = repo.last_offer_at()
    if last and tu.minutes_between(last, now) < config.GLOBAL_COOLDOWN:
        return Verdict(False, "cooldown")
    return Verdict(True)
