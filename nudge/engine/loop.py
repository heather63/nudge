"""The tick and the reply handlers — the only place with interesting logic."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from .. import config
from .. import timeutil as tu
from ..errors import AuthError
from ..io import templates
from ..io.commands import Command, parse_worked_minutes
from ..sheet import Assignment, Sheet, remaining_minutes
from ..store.repo import Repo
from . import capacity, policy, scoring
from .gaps import Gap, GapSource

log = logging.getLogger(__name__)

# send(text, offer=True) attaches the reply buttons in Discord.
Sender = Callable[..., Awaitable[None]]

OPEN_OFFER_TTL_HOURS = 12.0
AUTH_WARNING_COOLDOWN_HOURS = 6.0
LAST_AUTH_WARNING = "last_auth_warning_at"


class NagEngine:
    def __init__(self, repo: Repo, sheet: Sheet, gaps: GapSource, send: Sender) -> None:
        self.repo = repo
        self.sheet = sheet
        self.gaps = gaps
        self.send = send
        self._gap_end: datetime | None = None
        self._offered_this_gap: set[str] = set()

    # --- the tick --------------------------------------------------------
    async def tick(self, now: datetime | None = None) -> None:
        now = now or tu.now_utc()
        try:
            assignments = await self.read(now)
            await asyncio.to_thread(self.gaps.refresh, now)
        except AuthError as exc:
            await self._warn_auth(str(exc), now)
            return
        except Exception:  # noqa: BLE001 - an outage skips a tick, nothing more
            log.exception("couldn't reach Google; skipping this tick")
            return

        await self.capacity_check(assignments, now)
        self.repo.expire_open_offers(now, OPEN_OFFER_TTL_HOURS)

        gap = self.gaps.current_gap(now)
        if gap is None:
            return
        if gap.end != self._gap_end:
            self._gap_end = gap.end
            self._offered_this_gap = set()
        verdict = policy.may_nag(self.repo, gap, now)
        if not verdict.ok:
            log.debug("not nagging: %s", verdict.reason)
            return
        await self.offer_best(assignments, gap, now)

    async def read(self, now: datetime) -> list[Assignment]:
        return await asyncio.to_thread(self.sheet.read, now)

    async def _warn_auth(self, message: str, now: datetime) -> None:
        """DM about a broken token rather than dying quietly — but not every tick."""
        last = self.repo.kv_time(LAST_AUTH_WARNING)
        if last and tu.minutes_between(last, now) < AUTH_WARNING_COOLDOWN_HOURS * 60:
            log.warning("auth problem (already warned): %s", message)
            return
        self.repo.kv_set_time(LAST_AUTH_WARNING, now)
        await self.send(templates.auth_problem(message))

    async def capacity_check(
        self, assignments: list[Assignment], now: datetime, *, force: bool = False
    ) -> list:
        if not force and not capacity.due_today(self.repo, now):
            return []
        try:
            warnings = await asyncio.to_thread(
                capacity.daily_check, assignments, self.repo, now, self.gaps.free_intervals
            )
        except Exception:  # noqa: BLE001
            log.exception("capacity check failed")
            return []
        if warnings:
            await self.send(templates.capacity_warning(warnings, now))
        return warnings

    # --- offering --------------------------------------------------------
    def rank(self, assignments: list[Assignment], gap: Gap, now: datetime):
        return scoring.rank(
            assignments,
            now=now,
            usable_gap_min=gap.usable_min(now),
            states=self.repo.states(),
            exclude=self._offered_this_gap,
            availability=capacity.read_snapshot(self.repo, now),
        )

    async def offer_best(
        self, assignments: list[Assignment], gap: Gap, now: datetime
    ) -> Assignment | None:
        ranking = self.rank(assignments, gap, now)
        if not ranking:
            return None
        chosen = ranking[0].assignment
        usable = gap.usable_min(now)
        session = scoring.session_minutes(chosen.remaining_min, usable)
        offer_id = self.repo.record_offer(
            chosen.sheet_id, chosen.label, gap.start, gap.end, session, now
        )
        self._offered_this_gap.add(chosen.sheet_id)
        await self.send(templates.offer(chosen, session, usable, now, offer_id), offer=True)
        return chosen

    # --- replies ---------------------------------------------------------
    async def handle(self, command: Command, now: datetime | None = None) -> str | None:
        now = now or tu.now_utc()
        handler = getattr(self, f"_cmd_{command.name}", None)
        if handler is None:
            return templates.HELP
        return await handler(command, now)

    def _open_offer(self, now: datetime):
        return self.repo.open_offer(now, OPEN_OFFER_TTL_HOURS)

    async def _find(self, sheet_id: str, now: datetime) -> Assignment | None:
        return next((a for a in await self.read(now) if a.sheet_id == sheet_id), None)

    async def _record_progress(self, offer, a: Assignment, percent: float,
                               minutes: float, now: datetime) -> float:
        """Write the new % to the sheet and log it. Returns the clamped percent."""
        percent = max(0.0, min(percent, 100.0))
        await asyncio.to_thread(self.sheet.write_percent, a.sheet_id, percent)
        self.repo.resolve_offer(offer.id, "done" if percent >= 100 else "progress",
                                now, minutes)
        self.repo.clear_state(a.sheet_id)  # progress resets declines and snoozes
        return percent

    async def _cmd_done(self, command: Command, now: datetime) -> str:
        """The sitting nudge asked for — or the whole thing, if that was all that was left."""
        offer = self._open_offer(now)
        if offer is None:
            return templates.no_open_offer()
        a = await self._find(offer.sheet_id, now)
        if a is None:
            return templates.row_missing(offer.label)
        finishes = not a.expected_min or offer.session_min >= a.remaining_min - 0.5
        if finishes:
            await self._record_progress(offer, a, 100, offer.session_min, now)
            return templates.ack_done(a.label)
        gained = offer.session_min / a.expected_min * 100
        percent = await self._record_progress(
            offer, a, a.percent_done + gained, offer.session_min, now
        )
        return templates.ack_session_done(
            a.label, offer.session_min, percent, remaining_minutes(a.expected_min, percent)
        )

    async def _cmd_log_minutes(self, command: Command, now: datetime) -> str:
        offer = self._open_offer(now)
        if offer is None:
            return templates.no_open_offer()
        minutes = parse_worked_minutes(command.arg)
        if minutes is None:
            return "Didn't catch how long. Try `did 30` or `40%`."
        a = await self._find(offer.sheet_id, now)
        if a is None:
            return templates.row_missing(offer.label)
        if not a.expected_min:
            return templates.needs_expected_length(a.label)
        percent = await self._record_progress(
            offer, a, a.percent_done + minutes / a.expected_min * 100, minutes, now
        )
        return templates.ack_progress(
            a.label, percent, remaining_minutes(a.expected_min, percent)
        )

    async def _cmd_set_percent(self, command: Command, now: datetime) -> str:
        offer = self._open_offer(now)
        if offer is None:
            return templates.no_open_offer()
        a = await self._find(offer.sheet_id, now)
        if a is None:
            return templates.row_missing(offer.label)
        target = float(command.arg)
        expected = a.expected_min or config.DEFAULT_EXPECTED_MINUTES
        gained = max(target - a.percent_done, 0) / 100 * expected
        percent = await self._record_progress(offer, a, target, gained, now)
        return templates.ack_progress(
            a.label, percent, remaining_minutes(a.expected_min, percent)
        )

    async def _cmd_not_now(self, command: Command, now: datetime) -> str:
        offer = self._open_offer(now)
        if offer is None:
            return templates.no_open_offer()
        self.repo.resolve_offer(offer.id, "not_now", now)
        self.repo.kv_set_time(policy.NO_OFFER_UNTIL, max(offer.gap_end, now))
        if self.repo.bump_decline(offer.sheet_id) >= config.MAX_DECLINES:
            # Passing on it again and again isn't going to change from a nag.
            until = tu.parse_when("tomorrow", now=now)
            self.repo.clear_state(offer.sheet_id)
            self.repo.set_snooze(offer.sheet_id, until)
            return templates.ack_set_aside(offer.label, until)
        return templates.ack_not_now()

    async def _cmd_snooze(self, command: Command, now: datetime) -> str:
        offer = self._open_offer(now)
        if offer is None:
            return templates.no_open_offer()
        until = tu.parse_when(command.arg or "tomorrow", now=now) or tu.parse_when(
            "tomorrow", now=now
        )
        self.repo.resolve_offer(offer.id, "snooze", now)
        self.repo.set_snooze(offer.sheet_id, until)
        self.repo.kv_set_time(policy.NO_OFFER_UNTIL, max(offer.gap_end, now))
        return templates.ack_snooze(offer.label, until)

    async def _cmd_something_else(self, command: Command, now: datetime) -> str | None:
        offer = self._open_offer(now)
        if offer is not None:
            self.repo.resolve_offer(offer.id, "other", now)
            self._offered_this_gap.add(offer.sheet_id)
        gap = self.gaps.current_gap(now)
        if gap is None:
            last = self.repo.latest_offer(now, OPEN_OFFER_TTL_HOURS)
            if last and last.gap_end > now:
                gap = Gap(last.gap_start, last.gap_end)
        if gap is None:
            return "No free time on your calendar right now."
        # An explicit ask skips the cooldown and the daily budget.
        if await self.offer_best(await self.read(now), gap, now) is None:
            return "Nothing else worth starting in that gap."
        return None

    async def _cmd_busy(self, command: Command, now: datetime) -> str:
        """"busy until friday" — about you, not the assignment, so no decline is counted."""
        if command.arg.lower() in {"off", "over", "done", "no more"}:
            self.repo.kv_del(policy.BUSY_UNTIL)
            self.repo.kv_del(policy.NO_OFFER_UNTIL)
            return templates.ack_back()
        until = tu.parse_when(command.arg, now=now)
        understood = until is not None
        if until is None:
            until = now + timedelta(hours=config.DEFAULT_BUSY_HOURS)
        offer = self._open_offer(now)
        if offer is not None:
            self.repo.resolve_offer(offer.id, "not_now", now)
        self.repo.kv_set_time(policy.BUSY_UNTIL, until)
        return templates.ack_busy(until, understood)

    async def _cmd_status(self, command: Command, now: datetime) -> str:
        text = templates.status(
            await self.read(now), now, capacity.read_snapshot(self.repo, now)
        )
        busy_until = self.repo.kv_time(policy.BUSY_UNTIL)
        if busy_until and busy_until > now:
            text += f"\n_busy until {tu.fmt_when(busy_until)}_"
        text += f"\n_{self.repo.offers_today(now)}/{config.MAX_OFFERS_PER_DAY} offers today_"
        if self.sheet.url:
            text += f"\n{self.sheet.url}"
        return text

    async def _cmd_help(self, command: Command, now: datetime) -> str:
        return templates.HELP
