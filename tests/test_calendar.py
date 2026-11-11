"""Real calendar gaps and the daily capacity check."""
from __future__ import annotations

import unittest
import unittest.mock
from datetime import timedelta

from fakes import dt

from nudge import config
from nudge import timeutil as tu
from nudge.connectors.gcal import CalendarClient
from nudge.engine import capacity, scoring
from nudge.engine.gaps import CalendarGapSource, Gap, invert_busy
from nudge.sheet import Assignment
from nudge.store import db
from nudge.store.repo import Repo


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
        self.assertEqual(gap.usable_min(dt(13)), float(config.MAX_SESSION_MINUTES))

    def test_a_short_gap_is_untouched(self):
        gap = Gap(dt(12), dt(12, 40))
        self.assertEqual(gap.usable_min(dt(12)), 40 - config.TAIL_BUFFER)

    def test_fit_still_discriminates_inside_a_long_gap(self):
        # Without the cap every assignment would score 1.0 in a long gap and
        # ranking would collapse onto urgency alone.
        usable = Gap(dt(12), dt(20)).usable_min(dt(13))
        self.assertEqual(usable, float(config.MAX_SESSION_MINUTES))
        finishable = scoring.fit(90, usable)
        enormous = scoring.fit(60 * 20, usable)
        self.assertEqual(finishable, 1.0)
        self.assertLess(enormous, finishable)

    def test_the_session_asked_for_never_exceeds_the_cap(self):
        usable = Gap(dt(12), dt(20)).usable_min(dt(13))
        self.assertEqual(
            scoring.session_minutes(60 * 20, usable), config.MAX_SESSION_MINUTES
        )


class TestCalendarGapSource(unittest.TestCase):
    def source(self, busy):
        return CalendarGapSource(client=FakeBusySource(busy))

    def test_invert_busy_merges_overlaps(self):
        gaps = invert_busy(
            [(dt(10), dt(11)), (dt(10, 30), dt(12)), (dt(14), dt(15))], dt(9), dt(17)
        )
        self.assertEqual(
            [(g.start, g.end) for g in gaps],
            [(dt(9), dt(10)), (dt(12), dt(14)), (dt(15), dt(17))],
        )

    def test_invert_busy_clips_to_the_window(self):
        gaps = invert_busy([(dt(8), dt(9, 30))], dt(9), dt(10))
        self.assertEqual([(g.start, g.end) for g in gaps], [(dt(9, 30), dt(10))])

    def test_the_gap_you_are_in_runs_until_your_next_event(self):
        source = self.source([(dt(14), dt(15)), (dt(18), dt(19))])
        source.refresh(dt(16))
        gap = source.current_gap(dt(16))
        self.assertEqual(gap.end, dt(18))
        self.assertEqual(gap.usable_min(dt(16)), 120 - config.TAIL_BUFFER)

    def test_being_busy_means_no_current_gap(self):
        source = self.source([(dt(14), dt(18))])
        source.refresh(dt(16))
        self.assertIsNone(source.current_gap(dt(16)))

    def test_each_refresh_asks_the_calendar(self):
        source = self.source([])
        source.refresh(dt(16))
        source.refresh(dt(16, 10))
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


def assignment(name="Big project", hours=20.0, due=None, percent=0.0, sheet_id=None):
    return Assignment(
        sheet_id=sheet_id or f"id-{name}", name=name, course="CS 4262",
        due_at=due, expected_min=int(hours * 60), percent_done=percent,
    )


class TestDailyCheck(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(db.connect(":memory:"))
        self.addCleanup(self.repo.conn.close)
        self.now = dt(16)
        self.crammed = assignment(due=self.now + timedelta(days=1))

    def check(self, *assignments, free=None):
        return capacity.daily_check(list(assignments), self.repo, self.now, free)

    def test_it_warns_when_the_work_no_longer_fits(self):
        warnings = self.check(self.crammed)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].name, "CS 4262 · Big project")
        self.assertGreater(warnings[0].short_by, 0)

    def test_it_warns_only_once(self):
        self.assertEqual(len(self.check(self.crammed)), 1)
        self.assertEqual(self.check(self.crammed), [])

    def test_it_can_warn_again_after_it_fits_and_slips_back(self):
        self.check(self.crammed)
        self.crammed.percent_done = 99
        self.assertEqual(self.check(self.crammed), [])
        self.crammed.percent_done = 0
        self.assertEqual(len(self.check(self.crammed)), 1)

    def test_a_comfortable_assignment_is_silent(self):
        reading = assignment("Reading", hours=0.75, due=self.now + timedelta(days=14))
        self.assertEqual(self.check(reading), [])

    def test_assignments_with_no_due_date_are_skipped(self):
        self.assertEqual(self.check(assignment("Someday", hours=10)), [])

    def test_a_finished_assignment_is_silent(self):
        self.crammed.percent_done = 100
        self.assertEqual(self.check(self.crammed), [])

    def test_it_records_measured_availability_for_scoring(self):
        self.check(self.crammed, free=lambda start, end: [(start, end)])
        snapshot = capacity.read_snapshot(self.repo, self.now)
        self.assertGreater(snapshot["id-Big project"], 0)

    def test_a_stale_snapshot_is_ignored(self):
        self.check(self.crammed, free=lambda s, e: [(s, e)])
        later = self.now + timedelta(hours=capacity.SNAPSHOT_TTL_HOURS + 1)
        self.assertEqual(capacity.read_snapshot(self.repo, later), {})

    def test_a_freebusy_failure_falls_back_instead_of_crashing(self):
        def broken(start, end):
            raise RuntimeError("503")

        with self.assertLogs("nudge.engine.capacity", level="ERROR"):
            self.assertEqual(len(self.check(self.crammed, free=broken)), 1)

    def test_it_runs_once_a_day(self):
        self.assertTrue(capacity.due_today(self.repo, self.now))
        self.check(self.crammed)
        self.assertFalse(capacity.due_today(self.repo, self.now))
        self.assertTrue(capacity.due_today(self.repo, self.now + timedelta(days=1)))

    def test_it_queries_freebusy_once_for_the_furthest_due_date(self):
        calls = []

        def free(start, end):
            calls.append((start, end))
            return [(start, end)]

        rows = [assignment(f"A{i}", hours=2, due=self.now + timedelta(days=i + 1))
                for i in range(12)]
        self.check(*rows, free=free)
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(calls[0][1], self.now + timedelta(days=12))
        snapshot = capacity.read_snapshot(self.repo, self.now)
        values = [snapshot[a.sheet_id] for a in rows]
        self.assertEqual(values, sorted(values))  # nearer due dates, less time


class TestPressure(unittest.TestCase):
    def test_no_due_date_means_no_pressure(self):
        self.assertEqual(capacity.pressure(600, dt(12), None), 0.0)

    def test_overdue_outranks_everything(self):
        self.assertEqual(capacity.pressure(30, dt(12), dt(11)), float("inf"))

    def test_pressure_tracks_size_not_just_proximity(self):
        now = dt(12)
        big_far = capacity.pressure(600, now, now + timedelta(days=5))
        small_near = capacity.pressure(60, now, now + timedelta(days=2))
        self.assertGreater(big_far, small_near)

    def test_the_window_starts_when_the_work_opens(self):
        now = dt(16, day=7)
        due = now + timedelta(days=4)
        opens = now + timedelta(days=3, hours=22)
        self.assertLess(capacity.available_minutes(now, due, opens_at=opens),
                        capacity.available_minutes(now, due))


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


if __name__ == "__main__":
    unittest.main()
