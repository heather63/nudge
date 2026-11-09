"""Phase 3 (real gaps) and phase 5 (the daily capacity check)."""
from __future__ import annotations

import json
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

from nudge import config
from nudge import timeutil as tu
from nudge.connectors.gcal import CalendarClient
from nudge.engine import capacity, policy, scoring
from nudge.engine.gaps import CalendarGapSource, Gap
from nudge.store import db
from nudge.store.repo import Repo

UTC = timezone.utc


def dt(hour: int, minute: int = 0, day: int = 8) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


class Canned:
    """Minimal stand-in for a googleapiclient request."""

    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeCalendarService:
    def __init__(self, calendars: list[dict], freebusy: dict):
        self._calendars = calendars
        self._freebusy = freebusy
        self.queries: list[dict] = []

    def calendarList(self):
        return self

    def list(self, **kwargs):
        return Canned({"items": self._calendars})

    def freebusy(self):
        return self

    def query(self, body=None):
        self.queries.append(body)
        return Canned({"calendars": self._freebusy})


class FakeBusySource:
    """Injected into CalendarGapSource in place of CalendarClient."""

    def __init__(self, busy):
        self.busy_intervals = busy
        self.calls = 0

    def busy(self, time_min, time_max):
        self.calls += 1
        return list(self.busy_intervals)


class TestCalendarClient(unittest.TestCase):
    def test_a_calendar_that_errors_is_dropped_and_not_asked_again(self):
        service = FakeCalendarService(
            calendars=[
                {"id": "primary", "summary": "me"},
                {"id": "hol", "summary": "Holidays"},
            ],
            freebusy={
                "primary": {"busy": [{"start": tu.iso(dt(14)), "end": tu.iso(dt(15))}]},
                "hol": {"errors": [{"reason": "notFound"}]},
            },
        )
        client = CalendarClient(service)
        with self.assertLogs("nudge.connectors.gcal", level="WARNING"):
            busy = client.busy(dt(9), dt(21))

        self.assertEqual(busy, [(dt(14), dt(15))])
        self.assertIn("hol", client._broken)
        self.assertEqual([n for _, n in client.watched_calendars()], ["me"])

    def test_excluded_calendars_are_never_queried(self):
        service = FakeCalendarService(
            calendars=[{"id": "h", "summary": "Holidays in United States"}],
            freebusy={},
        )
        client = CalendarClient(service)
        client.busy(dt(9), dt(21))
        self.assertEqual(service.queries, [])  # nothing left to ask about

    def test_only_named_calendars_are_used_when_configured(self):
        service = FakeCalendarService(
            calendars=[
                {"id": "a", "summary": "CS 4262"},
                {"id": "b", "summary": "Extracirriculars"},
            ],
            freebusy={"a": {"busy": []}},
        )
        client = CalendarClient(service)
        with unittest.mock.patch.object(config, "CALENDARS", ("CS 4262",)):
            self.assertEqual([n for _, n in client.watched_calendars()], ["CS 4262"])


class TestGapCapping(unittest.TestCase):
    def test_a_long_gap_is_capped(self):
        gap = Gap(dt(12), dt(20))
        self.assertEqual(gap.usable_min(dt(13)), float(config.MAX_USABLE_GAP_MINUTES))

    def test_a_short_gap_is_untouched(self):
        gap = Gap(dt(12), dt(12, 40))
        self.assertEqual(gap.usable_min(dt(12)), 40 - config.TAIL_BUFFER)

    def test_fit_still_discriminates_inside_a_long_gap(self):
        # Without the cap every assignment would score 1.0 in a long gap and
        # ranking would collapse onto urgency alone.
        usable = Gap(dt(12), dt(20)).usable_min(dt(13))
        self.assertEqual(usable, float(config.MAX_USABLE_GAP_MINUTES))
        finishable = scoring.fit(90, usable)
        enormous = scoring.fit(60 * 20, usable)
        self.assertEqual(finishable, 1.0)
        self.assertLess(enormous, finishable)

    def test_the_session_asked_for_never_exceeds_the_cap(self):
        usable = Gap(dt(12), dt(20)).usable_min(dt(13))
        self.assertEqual(
            scoring.session_minutes(60 * 20, usable), config.MAX_USABLE_GAP_MINUTES
        )


class TestCalendarGapSource(unittest.TestCase):
    def source(self, busy, **kwargs):
        return CalendarGapSource(client=FakeBusySource(busy), **kwargs)

    def test_busy_intervals_invert_into_the_gap_you_are_in(self):
        source = self.source([(dt(14), dt(15)), (dt(18), dt(19))])
        source.refresh(dt(16))
        gap = source.current_gap(dt(16))
        self.assertEqual((gap.start, gap.end), (dt(15), dt(18)))

    def test_the_lookback_preserves_a_real_start_so_lead_in_passes(self):
        # Free since 15:00; it is now 16:00. Without the lookback the gap would
        # appear to start now and LEAD_IN_DELAY would block every offer.
        source = self.source([(dt(14), dt(15))])
        source.refresh(dt(16))
        gap = source.current_gap(dt(16))
        self.assertEqual(gap.start, dt(15))
        self.assertTrue(policy.gap_is_usable(gap, dt(16)).ok)

    def test_a_gap_that_genuinely_just_opened_is_still_delayed(self):
        source = self.source([(dt(14), dt(16))])
        source.refresh(dt(16, 1))
        gap = source.current_gap(dt(16, 1))
        self.assertEqual(gap.start, dt(16))
        self.assertFalse(policy.gap_is_usable(gap, dt(16, 1)).ok)
        self.assertTrue(policy.gap_is_usable(gap, dt(16, 6)).ok)

    def test_being_busy_means_no_current_gap(self):
        source = self.source([(dt(14), dt(18))])
        source.refresh(dt(16))
        self.assertIsNone(source.current_gap(dt(16)))

    def test_the_cache_prevents_a_query_on_every_call(self):
        source = self.source([], cache_seconds=600)
        source.refresh(dt(16))
        source.refresh(dt(16, 1))
        source.refresh(dt(16, 2))
        self.assertEqual(source._client.calls, 1)

    def test_the_cache_expires(self):
        source = self.source([], cache_seconds=60)
        source.refresh(dt(16))
        source.refresh(dt(16, 5))
        self.assertEqual(source._client.calls, 2)

    def test_leaving_the_window_forces_a_refetch(self):
        source = self.source([], window_hours=1, cache_seconds=99999)
        source.refresh(dt(16))
        source.refresh(dt(18))
        self.assertEqual(source._client.calls, 2)

    def test_free_intervals_covers_an_arbitrary_range(self):
        source = self.source([(dt(14), dt(15))])
        intervals = source.free_intervals(dt(12), dt(18))
        self.assertEqual(intervals, [(dt(12), dt(14)), (dt(15), dt(18))])


class TestWakingMinutes(unittest.TestCase):
    def test_a_full_day_is_waking_hours_only(self):
        start = dt(4)  # local midnight
        self.assertEqual(capacity.waking_minutes(start, start + timedelta(days=1)), 900)

    def test_quiet_hours_are_excluded(self):
        # 22:00 - 02:00 local, quiet from 23:00 -> one waking hour
        self.assertEqual(capacity.waking_minutes(dt(2), dt(6)), 60)

    def test_an_interval_wholly_inside_quiet_hours_counts_nothing(self):
        self.assertEqual(capacity.waking_minutes(dt(4), dt(8)), 0)

    def test_a_non_wrapping_quiet_window_still_works(self):
        with unittest.mock.patch.object(config, "QUIET_HOURS", (2, 6)):
            start = dt(4)  # local midnight
            self.assertEqual(
                capacity.waking_minutes(start, start + timedelta(days=1)), 1200
            )


class TestAvailability(unittest.TestCase):
    def test_measured_free_time_is_capped_per_day(self):
        now = dt(12)
        until = now + timedelta(days=3)
        wide_open = [(now, until)]
        available = capacity.available_minutes(now, until, wide_open)
        self.assertLessEqual(available, 3 * config.WORKABLE_HOURS_PER_DAY * 60)

    def test_a_busy_calendar_lowers_availability_below_the_cap(self):
        now = dt(12)
        until = now + timedelta(days=3)
        sparse = [(now, now + timedelta(hours=1))]
        self.assertLess(
            capacity.available_minutes(now, until, sparse),
            capacity.available_minutes(now, until, [(now, until)]),
        )

    def test_no_intervals_falls_back_to_the_flat_estimate(self):
        now = dt(12)
        until = now + timedelta(days=2)
        self.assertAlmostEqual(
            capacity.available_minutes(now, until),
            2 * config.WORKABLE_HOURS_PER_DAY * 60,
        )


class TestDailyCheck(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(db.connect(":memory:"))
        self.addCleanup(self.repo.conn.close)
        self.now = dt(16)

    def crammed(self, hours: int = 20, due_in_days: int = 1) -> int:
        return self.repo.create_assignment(
            "Big project",
            course="CS 4262",
            due_at=self.now + timedelta(days=due_in_days),
            expected_min=hours * 60,
        )

    def test_it_warns_when_the_work_no_longer_fits(self):
        self.crammed()
        warnings = capacity.daily_check(self.repo, self.now)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].name, "CS 4262 · Big project")
        self.assertGreater(warnings[0].short_by, 0)

    def test_it_warns_only_once(self):
        self.crammed()
        self.assertEqual(len(capacity.daily_check(self.repo, self.now)), 1)
        self.assertEqual(len(capacity.daily_check(self.repo, self.now)), 0)

    def test_it_can_warn_again_after_the_pile_fits_and_slips_back(self):
        assignment = self.crammed()
        capacity.daily_check(self.repo, self.now)
        self.repo.set_percent(assignment, 99)
        self.assertEqual(capacity.daily_check(self.repo, self.now), [])

        self.repo.set_percent(assignment, 0)
        self.assertEqual(len(capacity.daily_check(self.repo, self.now)), 1)

    def test_a_comfortable_assignment_is_silent(self):
        self.repo.create_assignment(
            "Reading", due_at=self.now + timedelta(days=14), expected_min=45
        )
        self.assertEqual(capacity.daily_check(self.repo, self.now), [])

    def test_assignments_with_no_due_date_are_skipped(self):
        self.repo.create_assignment("Someday", expected_min=600)
        self.assertEqual(capacity.daily_check(self.repo, self.now), [])

    def test_a_finished_assignment_is_silent(self):
        assignment = self.crammed()
        self.repo.set_percent(assignment, 100)
        self.assertEqual(capacity.daily_check(self.repo, self.now), [])

    def test_it_records_measured_availability_for_scoring(self):
        self.crammed()
        free = lambda start, end: [(start, end)]
        capacity.daily_check(self.repo, self.now, free)
        snapshot = capacity.read_snapshot(self.repo, self.now)
        self.assertEqual(len(snapshot), 1)
        self.assertGreater(next(iter(snapshot.values())), 0)

    def test_a_stale_snapshot_is_ignored(self):
        self.crammed()
        capacity.daily_check(self.repo, self.now, lambda s, e: [(s, e)])
        later = self.now + timedelta(hours=capacity.SNAPSHOT_TTL_HOURS + 1)
        self.assertEqual(capacity.read_snapshot(self.repo, later), {})

    def test_a_freebusy_failure_falls_back_instead_of_crashing(self):
        self.crammed()

        def broken(start, end):
            raise RuntimeError("503")

        with self.assertLogs("nudge.engine.capacity", level="ERROR"):
            warnings = capacity.daily_check(self.repo, self.now, broken)
        self.assertEqual(len(warnings), 1)

    def test_it_runs_once_a_day(self):
        self.assertTrue(capacity.due_today(self.repo, self.now))
        capacity.daily_check(self.repo, self.now)
        self.assertFalse(capacity.due_today(self.repo, self.now))
        self.assertTrue(capacity.due_today(self.repo, self.now + timedelta(days=1)))


if __name__ == "__main__":
    unittest.main()


class TestEngineIntegration(unittest.IsolatedAsyncioTestCase):
    """The tick must use real gaps without ever blocking the event loop."""

    def setUp(self):
        from nudge.engine.loop import NagEngine

        self.repo = Repo(db.connect(":memory:"))
        self.addCleanup(self.repo.conn.close)
        self.sent: list[str] = []
        # Free since 15:00 local-noonish; a class ran 14:00-15:00.
        self.source = CalendarGapSource(client=FakeBusySource([(dt(14), dt(15))]))
        self.engine = NagEngine(self.repo, self.source, self.record)
        self.assignment = self.repo.create_assignment(
            "HW1", course="CS 4262", due_at=dt(12, day=11), expected_min=90
        )

    async def record(self, text: str) -> None:
        self.sent.append(text)

    async def test_a_tick_offers_from_a_real_calendar_gap(self):
        await self.engine.tick(dt(16))
        self.assertTrue(any("HW1" in m for m in self.sent), self.sent)

    async def test_the_gap_refresh_happens_off_the_event_loop(self):
        # asyncio.to_thread means the blocking client is never called inline.
        loop_thread = __import__("threading").get_ident()
        seen: list[int] = []

        class ThreadSpy(FakeBusySource):
            def busy(self, time_min, time_max):
                seen.append(__import__("threading").get_ident())
                return []

        self.engine.gaps = CalendarGapSource(client=ThreadSpy([]))
        await self.engine.refresh_gaps(dt(16))
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], loop_thread)

    async def test_a_calendar_outage_does_not_stop_the_nagging(self):
        class Broken(FakeBusySource):
            def busy(self, time_min, time_max):
                raise RuntimeError("503 from Google")

        self.source.refresh(dt(16))            # prime the cache
        self.source._client = Broken([])
        self.source._fetched_at = None         # force a refetch that will fail

        with self.assertLogs("nudge.engine.loop", level="ERROR"):
            await self.engine.tick(dt(16, 30))
        self.assertTrue(any("HW1" in m for m in self.sent), self.sent)

    async def test_the_capacity_warning_is_sent_once_a_day(self):
        self.repo.update_assignment(self.assignment, expected_min=60 * 20)
        await self.engine.tick(dt(16))
        warnings = [m for m in self.sent if "no longer fits" in m]
        self.assertEqual(len(warnings), 1)

        self.sent.clear()
        self.repo.kv_set("last_offer_at", tu.iso(dt(1)))
        await self.engine.tick(dt(17))
        self.assertEqual([m for m in self.sent if "no longer fits" in m], [])

    async def test_measured_availability_reaches_the_score(self):
        capacity.write_snapshot(self.repo, dt(16), {self.assignment: 60.0})
        ranking = self.engine.rank_for(Gap(dt(15), dt(18)), dt(16))
        # 90 min of work against 60 * 0.6 = 36 usable minutes -> it does not fit
        self.assertEqual(ranking[0].parts["pressure"], 1.0)


class TestCapacityQueryCost(unittest.TestCase):
    """One freeBusy call per run, not one per assignment."""

    def setUp(self):
        self.repo = Repo(db.connect(":memory:"))
        self.addCleanup(self.repo.conn.close)
        self.now = dt(16)
        for i in range(12):
            self.repo.create_assignment(
                f"assignment {i}",
                due_at=self.now + timedelta(days=i + 1),
                expected_min=120,
            )
        self.calls = []

    def free(self, start, end):
        self.calls.append((start, end))
        return [(start, end)]

    def test_it_queries_freebusy_once(self):
        capacity.daily_check(self.repo, self.now, self.free)
        self.assertEqual(len(self.calls), 1)

    def test_the_single_window_covers_the_furthest_due_date(self):
        capacity.daily_check(self.repo, self.now, self.free)
        _start, end = self.calls[0]
        self.assertGreaterEqual(end, self.now + timedelta(days=12))

    def test_each_assignment_still_gets_its_own_window(self):
        capacity.daily_check(self.repo, self.now, self.free)
        snapshot = capacity.read_snapshot(self.repo, self.now)
        values = [snapshot[int(r["id"])] for r in self.repo.summary()]
        # Nearer due dates must measure less available time than further ones.
        self.assertEqual(values, sorted(values))
        self.assertLess(values[0], values[-1])

    def test_a_failure_still_falls_back_to_the_estimate(self):
        def broken(start, end):
            raise RuntimeError("503")

        with self.assertLogs("nudge.engine.capacity", level="ERROR"):
            capacity.daily_check(self.repo, self.now, broken)
        self.assertTrue(capacity.read_snapshot(self.repo, self.now))


class TestRetryWrapper(unittest.TestCase):
    """Transient failures must not skip a whole reconcile."""

    class Flaky:
        def __init__(self, failures, error):
            self.remaining = failures
            self.error = error
            self.calls = 0

        def execute(self):
            self.calls += 1
            if self.remaining > 0:
                self.remaining -= 1
                raise self.error
            return {"ok": True}

    def run_with(self, error, failures=2):
        from nudge.connectors import google_auth

        request = self.Flaky(failures, error)
        with unittest.mock.patch("time.sleep"):
            result = google_auth.execute(request)
        return result, request.calls

    def test_a_socket_timeout_is_retried(self):
        result, calls = self.run_with(TimeoutError("read timed out"))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, 3)

    def test_a_connection_error_is_retried(self):
        result, calls = self.run_with(ConnectionResetError("reset"))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, 3)

    def test_it_gives_up_eventually(self):
        from nudge.connectors import google_auth

        request = self.Flaky(99, TimeoutError("nope"))
        with unittest.mock.patch("time.sleep"):
            with self.assertRaises(TimeoutError):
                google_auth.execute(request, attempts=3)
        self.assertEqual(request.calls, 3)

    def test_a_bad_request_is_not_retried(self):
        from googleapiclient.errors import HttpError
        from nudge.connectors import google_auth

        error = HttpError(unittest.mock.Mock(status=400), b"bad")
        request = self.Flaky(99, error)
        with self.assertRaises(HttpError):
            google_auth.execute(request)
        self.assertEqual(request.calls, 1)
