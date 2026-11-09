"""The tick and the command handlers — the only place with interesting logic."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import config
from .. import timeutil as tu
from ..io import templates
from ..io.commands import Command, parse_worked_minutes
from ..store.repo import Candidate, Repo
from . import capacity, policy, scoring
from .gaps import FakeGapSource, Gap, GapSource

from ..errors import AuthError

log = logging.getLogger(__name__)

Sender = Callable[[str], Awaitable[None]]

OPEN_OFFER_TTL_HOURS = 12.0
AUTH_WARNING_COOLDOWN_HOURS = 6.0
LAST_AUTH_WARNING = "last_auth_warning_at"
SKIP_YEARS = 365  # "skip" is a very long snooze, so the sheet row is untouched


class NagEngine:
    def __init__(
        self,
        repo: Repo,
        gap_source: GapSource,
        send: Sender,
        mirror: Any | None = None,
    ) -> None:
        self.repo = repo
        self.gaps = gap_source
        self.send = send
        self.mirror = mirror  # nudge.mirror.Mirror, or None when offline
        self._gap_key: str | None = None
        self._offered_this_gap: set[int] = set()
        self._last_ranking: list[scoring.Scored] = []
        self._last_skipped: int | None = None
        self._last_sync: datetime | None = None
        self._background: set[asyncio.Task] = set()
        self._sync_lock = asyncio.Lock()

    # --- sheet + tasks sync ---------------------------------------------
    async def maybe_sync(self, now: datetime, *, force: bool = False):
        """Blocking clients, so the reconcile runs off the event loop thread."""
        if self.mirror is None:
            return None
        if not force and self._last_sync is not None:
            if tu.minutes_between(self._last_sync, now) < config.SHEET_SYNC_MINUTES:
                return None
        self._last_sync = now
        async with self._sync_lock:
            try:
                report = await asyncio.to_thread(self.mirror.sync)
            except AuthError as exc:
                await self._warn_auth(str(exc), now)
                return None
            except Exception:  # noqa: BLE001 - an outage must not stop nagging
                log.exception("mirror sync failed")
                return None
        if report.completed_elsewhere:
            await self.send(templates.completed_elsewhere(report.completed_elsewhere))
        if report.reopened_elsewhere:
            await self.send(templates.reopened_elsewhere(report.reopened_elsewhere))
        if report.new_from_sheet:
            await self.send(templates.new_from_sheet(report.new_from_sheet))
        if report.adopted_from_tasks:
            await self.send(templates.adopted_from_tasks(report.adopted_from_tasks))
        return report

    async def _warn_auth(self, message: str, now: datetime) -> None:
        """DM about a broken token rather than dying quietly (plan §12)."""
        last = self.repo.kv_time(LAST_AUTH_WARNING)
        if last and tu.minutes_between(last, now) < AUTH_WARNING_COOLDOWN_HOURS * 60:
            log.warning("auth problem (already warned): %s", message)
            return
        self.repo.kv_set_time(LAST_AUTH_WARNING, now)
        await self.send(templates.auth_problem(message))

    def _spawn_sync(self) -> None:
        """Fire-and-forget push so a reply reaches the sheet without blocking."""
        if self.mirror is None:
            return
        task = asyncio.create_task(self.maybe_sync(tu.now_utc(), force=True))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # --- calendar + capacity ---------------------------------------------
    async def refresh_gaps(self, now: datetime) -> None:
        refresh = getattr(self.gaps, "refresh", None)
        if refresh is None:
            return
        try:
            await asyncio.to_thread(refresh, now)
        except AuthError as exc:
            await self._warn_auth(str(exc), now)
        except Exception:  # noqa: BLE001 - keep nagging off the cached gaps
            log.exception("freeBusy refresh failed; using the cached window")

    async def capacity_check(self, now: datetime, *, force: bool = False) -> list:
        if not force and not capacity.due_today(self.repo, now):
            return []
        free_intervals_fn = getattr(self.gaps, "free_intervals", None)
        try:
            warnings = await asyncio.to_thread(
                capacity.daily_check, self.repo, now, free_intervals_fn
            )
        except Exception:  # noqa: BLE001
            log.exception("capacity check failed")
            return []
        if warnings:
            await self.send(templates.capacity_warning(warnings, now))
        return warnings

    def availability(self, now: datetime) -> dict[int, float]:
        return capacity.read_snapshot(self.repo, now)

    # --- the tick --------------------------------------------------------
    async def tick(self, now: datetime | None = None) -> None:
        now = now or tu.now_utc()
        await self.maybe_sync(now)
        await self.refresh_gaps(now)
        await self.capacity_check(now)
        self.repo.expire_open_offers(OPEN_OFFER_TTL_HOURS)

        gap = self.gaps.current_gap(now)
        if gap is None:
            return
        if gap.key() != self._gap_key:
            self._gap_key = gap.key()
            self._offered_this_gap = set()

        usable = policy.gap_is_usable(gap, now)
        if not usable.ok:
            log.debug("gap unusable: %s", usable.reason)
            return
        allowed = policy.may_nag(self.repo, now)
        if not allowed.ok:
            log.debug("not nagging: %s", allowed.reason)
            return

        await self.offer_best(gap, now)

    # --- offering --------------------------------------------------------
    def rank_for(self, gap: Gap, now: datetime) -> list[scoring.Scored]:
        ranking = scoring.rank(
            self.repo.candidates(),
            now=now,
            usable_gap_min=gap.usable_min(now),
            continuity_assignment_id=self.repo.recent_worked_assignment(
                config.CONTINUITY_WINDOW_HOURS
            ),
            last_decline_lookup=self.repo.last_decline_at,
            exclude=self._offered_this_gap,
            availability=self.availability(now),
        )
        self._last_ranking = ranking
        return ranking

    async def offer_best(self, gap: Gap, now: datetime) -> Candidate | None:
        ranking = self.rank_for(gap, now)
        if not ranking:
            return None

        chosen = ranking[0].candidate
        level = policy.escalation_level(chosen.decline_count)
        final = policy.is_final_offer(chosen.decline_count)
        session = scoring.session_minutes(chosen.remaining_min, gap.usable_min(now))

        offer_id = self.repo.record_offer(
            chosen.assignment_id, gap.start, gap.end, session
        )
        self._offered_this_gap.add(chosen.assignment_id)
        await self.send(
            templates.offer(chosen, level, session, gap.usable_min(now), now)
        )

        if final:
            # Six passes in a row is not work that is going to happen from a
            # nag. The sheet row is left alone; nudge just stops asking.
            self._skip(chosen.assignment_id, now)
            self.repo.resolve_offer(offer_id, "skip")
        return chosen

    def _skip(self, assignment_id: int, now: datetime) -> None:
        self.repo.set_snooze(assignment_id, now + timedelta(days=SKIP_YEARS))
        self._last_skipped = assignment_id

    def _active_gap(self, now: datetime) -> Gap | None:
        gap = self.gaps.current_gap(now)
        if gap is not None:
            return gap
        last = self.repo.latest_offer(within_hours=OPEN_OFFER_TTL_HOURS)
        if last and last.gap_end > now:
            return Gap(last.gap_start, last.gap_end, source="recalled")
        return None

    # --- command handling -------------------------------------------------
    async def handle(self, command: Command, now: datetime | None = None) -> str | None:
        now = now or tu.now_utc()
        handler = getattr(self, f"_cmd_{command.name}", None)
        if handler is None:
            return templates.HELP
        return await handler(command, now)

    def _open_offer(self, now: datetime):
        return self.repo.latest_offer(within_hours=OPEN_OFFER_TTL_HOURS)

    async def _cmd_done(self, command: Command, now: datetime) -> str:
        """"done" means what the situation makes it mean.

        If the sitting nudge asked for was the whole of what was left, it is
        finished. If it was a slice of something larger, "done" means the slice
        — closing an eight-hour project because you did two hours of it is not
        a mistake worth leaving available.
        """
        return await self._complete(now, force=False)

    async def _cmd_finish_all(self, command: Command, now: datetime) -> str:
        return await self._complete(now, force=True)

    async def _complete(self, now: datetime, *, force: bool) -> str:
        from ..store.repo import remaining_minutes

        offer = self._open_offer(now)
        if offer is None:
            return templates.no_open_offer()
        row = self.repo.get_assignment(offer.assignment_id)
        label = _label(row)

        remaining = remaining_minutes(row["expected_min"], row["percent_done"])
        # With no expected length there is no basis for reading "done" as a
        # slice, so it keeps its plain meaning.
        finishes = (
            force
            or not row["expected_min"]
            or offer.session_min >= remaining - 0.5
        )

        self.repo.reset_declines(offer.assignment_id)
        if finishes:
            self.repo.resolve_offer(offer.id, "done", float(offer.session_min))
            self.repo.set_percent(offer.assignment_id, 100)
            self.repo.clear_snooze(offer.assignment_id)
            self._spawn_sync()
            return templates.ack_done(label)

        percent, left = self.repo.add_progress(
            offer.assignment_id, float(offer.session_min)
        )
        self.repo.resolve_offer(offer.id, "progress", float(offer.session_min))
        self._spawn_sync()
        return templates.ack_session_done(label, offer.session_min, percent, left)

    async def _cmd_log_minutes(self, command: Command, now: datetime) -> str:
        offer = self._open_offer(now)
        if offer is None:
            return templates.no_open_offer()
        minutes = parse_worked_minutes(command.arg)
        if minutes is None:
            return "Didn't catch how long. Try `did 30` or `40%`."
        row = self.repo.get_assignment(offer.assignment_id)
        if not row["expected_min"]:
            return templates.needs_expected_length(_label(row))
        percent, remaining = self.repo.add_progress(offer.assignment_id, minutes)
        self.repo.resolve_offer(offer.id, "progress", minutes)
        self.repo.reset_declines(offer.assignment_id)
        self._spawn_sync()
        return templates.ack_progress(_label(row), percent, remaining)

    async def _cmd_set_percent(self, command: Command, now: datetime) -> str:
        offer = self._open_offer(now)
        if offer is None:
            return templates.no_open_offer()
        try:
            percent = float(command.arg)
        except ValueError:
            return "Didn't catch that percentage."
        row = self.repo.get_assignment(offer.assignment_id)
        before = float(row["percent_done"] or 0)
        percent = self.repo.set_percent(offer.assignment_id, percent)
        gained = max(percent - before, 0) / 100.0 * (
            row["expected_min"] or config.DEFAULT_EXPECTED_MINUTES
        )
        self.repo.resolve_offer(offer.id, "progress", gained)
        self.repo.reset_declines(offer.assignment_id)
        from ..store.repo import remaining_minutes

        self._spawn_sync()
        return templates.ack_progress(
            _label(row), percent, remaining_minutes(row["expected_min"], percent)
        )

    async def _cmd_not_now(self, command: Command, now: datetime) -> str:
        offer = self._open_offer(now)
        if offer is None:
            return templates.no_open_offer()
        self.repo.resolve_offer(offer.id, "not_now")
        self.repo.bump_decline(offer.assignment_id)
        self.repo.kv_set_time(policy.NO_OFFER_UNTIL, max(offer.gap_end, now))
        return templates.ack_not_now()

    async def _cmd_snooze(self, command: Command, now: datetime) -> str:
        offer = self._open_offer(now)
        if offer is None:
            return templates.no_open_offer()
        until = tu.parse_when(command.arg, now=now) or (now + timedelta(hours=2))
        self.repo.resolve_offer(offer.id, "snooze")
        self.repo.set_snooze(offer.assignment_id, until)
        self.repo.kv_set_time(policy.NO_OFFER_UNTIL, max(offer.gap_end, now))
        row = self.repo.get_assignment(offer.assignment_id)
        return templates.ack_snooze(_label(row), until)

    async def _cmd_skip(self, command: Command, now: datetime) -> str:
        offer = self._open_offer(now)
        if offer is None:
            return templates.no_open_offer()
        row = self.repo.get_assignment(offer.assignment_id)
        self.repo.resolve_offer(offer.id, "skip")
        self._skip(offer.assignment_id, now)
        return templates.ack_skip(_label(row))

    async def _cmd_undo(self, command: Command, now: datetime) -> str:
        if self._last_skipped is None:
            return "Nothing to undo."
        row = self.repo.get_assignment(self._last_skipped)
        self.repo.clear_snooze(self._last_skipped)
        self.repo.reset_declines(self._last_skipped)
        # Undo means "consider it again", including in the gap it was declined in.
        self._offered_this_gap.discard(self._last_skipped)
        self._last_skipped = None
        return templates.ack_undo(_label(row))

    async def _cmd_something_else(self, command: Command, now: datetime) -> str | None:
        offer = self._open_offer(now)
        if offer is not None and offer.response is None:
            self.repo.resolve_offer(offer.id, "not_now")
            self._offered_this_gap.add(offer.assignment_id)
        gap = self._active_gap(now)
        if gap is None:
            return "No gap open right now."
        # An explicit ask bypasses cooldown and the daily budget.
        chosen = await self.offer_best(gap, now)
        if chosen is None:
            return "Nothing else worth starting in that gap."
        return None

    async def _cmd_busy(self, command: Command, now: datetime) -> str:
        """"busy until friday" — a decline and a mute in one reply.

        No decline penalty: being unavailable is not a judgement on the work,
        and penalising it would push the assignment down the ranking for
        reasons that have nothing to do with the assignment.
        """
        until = tu.parse_when(command.arg, now=now)
        understood = until is not None
        if until is None:
            until = now + timedelta(hours=config.DEFAULT_BUSY_HOURS)
        offer = self._open_offer(now)
        if offer is not None and offer.response is None:
            self.repo.resolve_offer(offer.id, "not_now")
        self.repo.kv_set_time(policy.MUTE_UNTIL, until)
        return templates.ack_busy(until, understood)

    async def _cmd_mute(self, command: Command, now: datetime) -> str:
        until = tu.parse_when(command.arg, now=now) or (now + timedelta(hours=3))
        self.repo.kv_set_time(policy.MUTE_UNTIL, until)
        return templates.ack_mute(until)

    async def _cmd_unmute(self, command: Command, now: datetime) -> str:
        self.repo.kv_del(policy.MUTE_UNTIL)
        self.repo.kv_del(policy.NO_OFFER_UNTIL)
        return templates.ack_unmute()

    async def _cmd_status(self, command: Command, now: datetime) -> str:
        text = templates.status(self.repo.summary(), now, self.availability(now))
        mute_until = self.repo.kv_time(policy.MUTE_UNTIL)
        if mute_until and mute_until > now:
            text += f"\n_muted until {tu.to_local(mute_until):%a %-I:%M%p}_".lower()
        text += (
            f"\n_{self.repo.offers_today()}/{config.MAX_OFFERS_PER_DAY} offers today_"
        )
        if self.repo.open_proposal_count():
            text += "\n_an estimate is waiting on `approve`_"
        return text

    async def _cmd_sheet(self, command: Command, now: datetime) -> str:
        url = getattr(self.mirror, "url", None) if self.mirror else None
        return url or "No spreadsheet yet — run a sync first."

    async def _cmd_sync(self, command: Command, now: datetime) -> str:
        if self.mirror is None:
            return "The sheet isn't wired up (no Google credentials)."
        report = await self.maybe_sync(now, force=True)
        return f"Synced: {report}." if report else "Sync failed — check the logs."

    async def _cmd_capacity(self, command: Command, now: datetime) -> str | None:
        warnings = await self.capacity_check(now, force=True)
        if warnings:
            return None  # capacity_check already sent the detail
        return "Everything still fits in the time before it's due."

    async def _cmd_help(self, command: Command, now: datetime) -> str:
        return templates.HELP

    async def _cmd_gap(self, command: Command, now: datetime) -> str | None:
        if not isinstance(self.gaps, FakeGapSource):
            return "Gap injection only works with the fake gap source."
        try:
            minutes = int(command.arg.split()[0])
        except (ValueError, IndexError):
            minutes = 30
        gap = self.gaps.inject(minutes, now)
        self._gap_key = gap.key()
        self._offered_this_gap = set()
        self.repo.kv_del(policy.NO_OFFER_UNTIL)
        chosen = await self.offer_best(gap, now)
        if chosen is None:
            return f"Injected a {minutes} min gap — nothing worth starting."
        return None

    async def _cmd_why(self, command: Command, now: datetime) -> str:
        if not self._last_ranking:
            return "No ranking computed yet."
        lines = ["**Last ranking**"]
        for scored in self._last_ranking[:5]:
            parts = " ".join(f"{k}={v:.2f}" for k, v in scored.parts.items())
            lines.append(f"`{scored.score:+.3f}` {scored.candidate.label} — {parts}")
        return "\n".join(lines)

    # --- ingestion --------------------------------------------------------
    async def ingest(self, folder: Path) -> None:
        from ..ingest import pipeline
        from ..ingest.estimate import EstimateError

        course, name = pipeline.folder_identity(Path(folder))
        label = f"{course} · {name}" if course else name
        try:
            result = await asyncio.to_thread(pipeline.ingest_folder, self.repo, folder)
        except EstimateError as exc:
            await self.send(templates.ingest_failed(label, str(exc)))
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("ingest failed for %s", folder)
            await self.send(templates.ingest_failed(label, f"{type(exc).__name__}: {exc}"))
            return
        if result is None:
            log.info("nothing new for %s", label)
            return
        await self.send(templates.proposal(result.label, result.payload))

    async def _cmd_approve(self, command: Command, now: datetime) -> str:
        from ..ingest import pipeline

        row = self.repo.open_proposal()
        if row is None:
            return templates.nothing_to_approve()
        pipeline.approve(self.repo, row)
        self._spawn_sync()
        return templates.ack_approved(_proposal_label(row))

    async def _cmd_reject(self, command: Command, now: datetime) -> str:
        row = self.repo.open_proposal()
        if row is None:
            return templates.nothing_to_approve()
        self.repo.resolve_proposal(int(row["id"]), "rejected")
        return templates.ack_rejected(_proposal_label(row))

    async def _cmd_ingest(self, command: Command, now: datetime) -> str | None:
        from ..ingest import pipeline

        if not command.arg.strip():
            return "Name a folder, e.g. `ingest CS 4262/HW1`."
        folder = pipeline.watch_root() / command.arg.strip()
        if not folder.is_dir():
            return f"No folder at `{folder}`."
        await self.ingest(folder)
        return None


def _label(row) -> str:
    return f"{row['course']} · {row['name']}" if row["course"] else row["name"]


def _proposal_label(row) -> str:
    return (
        f"{row['course']} · {row['assignment_name']}"
        if row["course"]
        else row["assignment_name"]
    )
