"""Unit tests for the parts that are easy to get subtly wrong."""
from __future__ import annotations

import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

from nudge import config
from nudge import timeutil as tu
from nudge.engine import capacity, policy, scoring
from nudge.engine.gaps import FakeGapSource, Gap, invert_busy
from nudge.engine.loop import NagEngine
from nudge.io.commands import parse, parse_worked_minutes
from nudge.store import db
from nudge.store.repo import Repo, remaining_minutes

UTC = timezone.utc


def dt(hour: int, minute: int = 0, day: int = 8) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


class TestGaps(unittest.TestCase):
    def test_invert_busy_merges_overlaps(self):
        gaps = invert_busy(
            [(dt(10), dt(11)), (dt(10, 30), dt(12)), (dt(14), dt(15))], dt(9), dt(17)
        )
        self.assertEqual(
            [(g.start, g.end) for g in gaps],
            [(dt(9), dt(10)), (dt(12), dt(14)), (dt(15), dt(17))],
        )

    def test_invert_busy_clips_to_window(self):
        gaps = invert_busy([(dt(8), dt(9, 30))], dt(9), dt(10))
        self.assertEqual([(g.start, g.end) for g in gaps], [(dt(9, 30), dt(10))])

    def test_usable_min_subtracts_tail_buffer(self):
        self.assertEqual(Gap(dt(9), dt(10)).usable_min(dt(9, 30)), 30 - config.TAIL_BUFFER)

    def test_fake_source_is_deterministic(self):
        source = FakeGapSource(every_min=60, lengths=(30,), epoch=dt(9))
        self.assertIsNotNone(source.current_gap(dt(9, 10)))
        self.assertIsNone(source.current_gap(dt(9, 45)))
        self.assertIsNotNone(source.current_gap(dt(10, 5)))


class TestPolicy(unittest.TestCase):
    def test_quiet_hours_wrap_midnight(self):
        self.assertTrue(policy.in_quiet_hours(dt(4)))     # 00:00 local
        self.assertFalse(policy.in_quiet_hours(dt(16)))   # 12:00 local
        self.assertTrue(policy.in_quiet_hours(dt(3, 30)))  # 23:30 local

    def test_lead_in_delay_blocks_the_instant_a_gap_opens(self):
        gap = Gap(dt(16), dt(17))
        self.assertFalse(policy.gap_is_usable(gap, dt(16)).ok)
        self.assertTrue(policy.gap_is_usable(gap, dt(16, 5)).ok)

    def test_short_gap_rejected(self):
        self.assertFalse(policy.gap_is_usable(Gap(dt(16), dt(16, 18)), dt(16, 5)).ok)

    def test_tail_of_gap_rejected(self):
        self.assertFalse(policy.gap_is_usable(Gap(dt(16), dt(17)), dt(16, 50)).ok)

    def test_escalation_counts_declines_not_offers(self):
        # A twenty-hour project is legitimately offered many times.
        self.assertEqual(policy.escalation_level(0), 1)
        self.assertEqual(policy.escalation_level(2), 3)
        self.assertEqual(policy.escalation_level(99), config.MAX_ESCALATION)
        self.assertTrue(policy.is_final_offer(config.MAX_ESCALATION - 1))
        self.assertFalse(policy.is_final_offer(0))


class TestScoring(unittest.TestCase):
    def test_fit_rewards_being_able_to_finish(self):
        self.assertEqual(scoring.fit(30, 120), 1.0)
        self.assertEqual(scoring.fit(120, 120), 1.0)

    def test_fit_degrades_but_never_disqualifies(self):
        # Time-boxed work always makes some progress, so nothing scores zero.
        big = scoring.fit(1200, 120)
        medium = scoring.fit(240, 120)
        self.assertLess(big, medium)
        self.assertGreater(big, 0.0)

    def test_session_is_the_smaller_of_gap_and_remaining(self):
        self.assertEqual(scoring.session_minutes(240, 120), 120)
        self.assertEqual(scoring.session_minutes(25, 120), 25)

    def test_urgency_rises_toward_the_due_date(self):
        now = dt(12)
        far = scoring.urgency(now + timedelta(days=7), now)
        near = scoring.urgency(now + timedelta(hours=6), now)
        self.assertLess(far, near)
        self.assertEqual(scoring.urgency(now + timedelta(hours=1), now), 1.0)
        self.assertEqual(scoring.urgency(None, now), config.NULL_DUE_URGENCY)

    def test_decline_penalty_decays(self):
        now = dt(12)
        fresh = scoring.decline_penalty(2, now - timedelta(minutes=1), now)
        stale = scoring.decline_penalty(2, now - timedelta(hours=24), now)
        self.assertGreater(fresh, stale)

    def test_staleness_saturates_at_a_week(self):
        now = dt(12)
        self.assertEqual(scoring.staleness(None, now - timedelta(days=30), now), 1.0)


class TestRemaining(unittest.TestCase):
    def test_remaining_tracks_percent(self):
        self.assertEqual(remaining_minutes(180, 0), 180)
        self.assertEqual(remaining_minutes(180, 50), 90)
        self.assertEqual(remaining_minutes(180, 100), 0)

    def test_a_blank_expected_length_uses_the_default(self):
        self.assertEqual(remaining_minutes(None, 0), config.DEFAULT_EXPECTED_MINUTES)


class TestCommands(unittest.TestCase):
    def test_tolerates_extra_words(self):
        self.assertEqual(parse("ok done, that took forever").name, "done")
        self.assertEqual(parse("ehh not now").name, "not_now")
        self.assertEqual(parse("snooze until 3pm").arg, "until 3pm")
        self.assertIsNone(parse("just some notes to myself"))

    def test_progress_replies_beat_done(self):
        self.assertEqual(parse("did 30").name, "log_minutes")
        self.assertEqual(parse("did it").name, "done")
        self.assertEqual(parse("40%").name, "set_percent")
        self.assertEqual(parse("40%").arg, "40")

    def test_worked_minutes_reads_bare_numbers_as_minutes(self):
        self.assertEqual(parse_worked_minutes("30"), 30)
        self.assertEqual(parse_worked_minutes("45 min"), 45)
        self.assertEqual(parse_worked_minutes("2h"), 120)
        self.assertIsNone(parse_worked_minutes("later"))

    def test_parse_when(self):
        now = dt(12)
        self.assertEqual(tu.parse_when("90m", now=now), now + timedelta(minutes=90))
        self.assertIsNotNone(tu.parse_when("tomorrow", now=now))
        self.assertIsNone(tu.parse_when("whenever"))


class TestCapacity(unittest.TestCase):
    def test_no_due_date_means_no_pressure(self):
        self.assertEqual(capacity.pressure(600, dt(12), None), 0.0)

    def test_overdue_is_maximum_pressure(self):
        self.assertEqual(capacity.pressure(30, dt(12), dt(11)), 1.0)

    def test_pressure_tracks_size_not_just_proximity(self):
        now = dt(12)
        big_far = capacity.pressure(600, now, now + timedelta(days=5))
        small_near = capacity.pressure(60, now, now + timedelta(days=2))
        self.assertGreater(big_far, small_near)


class EngineFixture(unittest.IsolatedAsyncioTestCase):
    """Shared setup only — no tests, so subclasses don't re-run the suite."""

    def setUp(self):
        self.repo = Repo(db.connect(":memory:"))
        self.addCleanup(self.repo.conn.close)
        self.sent: list[str] = []
        self.source = FakeGapSource(every_min=600, lengths=(180,), epoch=dt(16))
        self.engine = NagEngine(self.repo, self.source, self.record)

    async def record(self, text: str) -> None:
        self.sent.append(text)

    def add(self, name: str, expected: int = 120, **kwargs) -> int:
        kwargs.setdefault("due_at", dt(23, 0, day=10))
        return self.repo.create_assignment(name, expected_min=expected, **kwargs)


class EngineCase(EngineFixture):
    async def test_tick_offers_one_assignment_only(self):
        self.add("A")
        self.add("B")
        await self.engine.tick(dt(16, 10))
        self.assertEqual(len(self.sent), 1)

    async def test_the_offer_names_a_session_length(self):
        self.add("A", expected=45)
        await self.engine.tick(dt(16, 10))
        self.assertIn("45 min", self.sent[0])

    async def test_a_long_assignment_is_boxed_to_the_gap(self):
        self.add("A", expected=60 * 20)
        await self.engine.tick(dt(16, 10))
        # (a 20-hour assignment also trips the capacity warning, hence the search)
        offer = next(m for m in self.sent if "**A**" in m and "?" in m)
        self.assertIn(tu.fmt_duration(config.MAX_USABLE_GAP_MINUTES), offer)

    async def test_cooldown_blocks_the_next_tick(self):
        self.add("A")
        await self.engine.tick(dt(16, 10))
        await self.engine.tick(dt(16, 20))
        self.assertEqual(len(self.sent), 1)

    async def test_quiet_hours_block_everything(self):
        self.add("A")
        source = FakeGapSource(every_min=600, lengths=(180,), epoch=dt(3))
        engine = NagEngine(self.repo, source, self.record)
        await engine.tick(dt(3, 10))  # 23:10 local
        self.assertEqual(self.sent, [])

    async def test_done_sets_it_to_a_hundred_percent(self):
        assignment = self.add("A")
        await self.engine.tick(dt(16, 10))
        reply = await self.engine.handle(parse("done"), dt(16, 20))
        self.assertIn("100%", reply)
        row = self.repo.get_assignment(assignment)
        self.assertEqual(row["percent_done"], 100)
        self.assertEqual(row["status"], "done")

    async def test_logging_minutes_advances_the_percentage(self):
        assignment = self.add("A", expected=120)
        await self.engine.tick(dt(16, 10))
        reply = await self.engine.handle(parse("did 30"), dt(16, 45))
        self.assertIn("25%", reply)
        self.assertAlmostEqual(self.repo.get_assignment(assignment)["percent_done"], 25)

    async def test_logging_past_the_end_completes_it(self):
        assignment = self.add("A", expected=60)
        await self.engine.tick(dt(16, 10))
        await self.engine.handle(parse("did 90"), dt(16, 45))
        self.assertEqual(self.repo.get_assignment(assignment)["percent_done"], 100)

    async def test_setting_a_percentage_directly(self):
        assignment = self.add("A", expected=120)
        await self.engine.tick(dt(16, 10))
        await self.engine.handle(parse("60%"), dt(16, 45))
        self.assertEqual(self.repo.get_assignment(assignment)["percent_done"], 60)

    async def test_time_cannot_be_logged_without_an_expected_length(self):
        self.add("A", expected=None)
        await self.engine.tick(dt(16, 10))
        reply = await self.engine.handle(parse("did 30"), dt(16, 45))
        self.assertIn("expected length", reply)

    async def test_progress_is_recorded_in_the_offers_log(self):
        self.add("A", expected=120)
        await self.engine.tick(dt(16, 10))
        await self.engine.handle(parse("did 30"), dt(16, 45))
        row = self.repo.conn.execute(
            "SELECT response, session_min, minutes_logged FROM offers"
        ).fetchone()
        self.assertEqual(row["response"], "progress")
        self.assertEqual(row["minutes_logged"], 30)

    async def test_progress_resets_the_decline_count(self):
        assignment = self.add("A")
        self.repo.bump_decline(assignment)
        self.repo.bump_decline(assignment)
        await self.engine.tick(dt(16, 10))
        await self.engine.handle(parse("did 20"), dt(16, 45))
        self.assertEqual(self.repo.candidates()[0].decline_count, 0)

    async def test_not_now_silences_the_rest_of_the_gap(self):
        self.add("A")
        self.add("B")
        await self.engine.tick(dt(16, 10))
        await self.engine.handle(parse("not now"), dt(16, 12))
        self.repo.kv_set_time("last_offer_at", dt(1))
        await self.engine.tick(dt(16, 20))
        self.assertEqual(len(self.sent), 1)

    async def test_something_else_offers_the_next_one(self):
        self.add("A")
        self.add("B")
        await self.engine.tick(dt(16, 10))
        await self.engine.handle(parse("something else"), dt(16, 12))
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(self.sent[0], self.sent[1])

    async def test_snooze_takes_it_out_of_the_running(self):
        self.add("A")
        await self.engine.tick(dt(16, 10))
        await self.engine.handle(parse("snooze 2h"), dt(16, 12))
        self.assertEqual(self.engine.rank_for(Gap(dt(16), dt(19)), dt(16, 30)), [])

    async def test_skip_leaves_the_sheet_row_alone(self):
        assignment = self.add("A")
        await self.engine.tick(dt(16, 10))
        await self.engine.handle(parse("skip"), dt(16, 12))
        row = self.repo.get_assignment(assignment)
        self.assertEqual(row["status"], "active")       # still in the sheet
        self.assertEqual(row["percent_done"], 0)
        self.assertEqual(self.engine.rank_for(Gap(dt(16), dt(19)), dt(16, 30)), [])

    async def test_undo_puts_it_back(self):
        self.add("A")
        await self.engine.tick(dt(16, 10))
        await self.engine.handle(parse("skip"), dt(16, 12))
        await self.engine.handle(parse("undo"), dt(16, 13))
        self.assertEqual(len(self.engine.rank_for(Gap(dt(16), dt(19)), dt(16, 30))), 1)

    async def test_six_declines_in_a_row_stops_the_nagging(self):
        assignment = self.add("A")
        for _ in range(config.MAX_ESCALATION):
            self.repo.kv_set_time("last_offer_at", dt(1))
            self.repo.kv_del(policy.NO_OFFER_UNTIL)
            self.engine._offered_this_gap.clear()
            await self.engine.offer_best(Gap(dt(16), dt(19)), dt(16, 10))
            await self.engine.handle(parse("not now"), dt(16, 11))
        self.assertIn("stop bringing it up", self.sent[-1])
        self.assertEqual(self.engine.rank_for(Gap(dt(16), dt(19)), dt(16, 30)), [])

    async def test_daily_budget_is_enforced(self):
        for i in range(config.MAX_OFFERS_PER_DAY + 2):
            self.add(f"assignment {i}")
        now = dt(13)
        for _ in range(config.MAX_OFFERS_PER_DAY + 2):
            self.repo.kv_set_time("last_offer_at", dt(1))
            self.repo.kv_del(policy.NO_OFFER_UNTIL)
            engine = NagEngine(
                self.repo, FakeGapSource(every_min=600, lengths=(180,), epoch=now),
                self.record,
            )
            await engine.tick(now + timedelta(minutes=10))
            now += timedelta(minutes=60)
        self.assertEqual(len(self.sent), config.MAX_OFFERS_PER_DAY)

    async def test_mute_blocks_and_unmute_releases(self):
        self.add("A")
        await self.engine.handle(parse("mute 3h"), dt(16, 0))
        await self.engine.tick(dt(16, 10))
        self.assertEqual(self.sent, [])
        await self.engine.handle(parse("unmute"), dt(16, 15))
        await self.engine.tick(dt(16, 20))
        self.assertTrue(self.sent)

    async def test_a_finished_assignment_drops_out(self):
        assignment = self.add("A")
        self.repo.set_percent(assignment, 100)
        await self.engine.tick(dt(16, 10))
        self.assertEqual(self.sent, [])

    async def test_a_big_distant_assignment_outranks_a_small_near_one(self):
        self.add("10h project", expected=600, due_at=dt(12, day=13))
        self.add("1h reading", expected=60, due_at=dt(12, day=10))
        ranking = self.engine.rank_for(Gap(dt(16), dt(19)), dt(16, 10))
        self.assertEqual(ranking[0].candidate.name, "10h project")

    async def test_status_shows_percentages(self):
        assignment = self.add("A", expected=120)
        self.repo.set_percent(assignment, 25)
        reply = await self.engine.handle(parse("status"), dt(16))
        self.assertIn("25%", reply)
        self.assertIn("1h30 left", reply)


class TestDurability(unittest.TestCase):
    """A lost WAL silently empties the mirror — make sure it can't linger."""

    def test_data_survives_a_checkpoint_and_reopen(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nudge.db"
            first = db.connect(path)
            Repo(first).create_assignment("HW1", course="CS 4262")
            db.close(first)

            self.assertFalse((Path(tmp) / "nudge.db-wal").exists())
            second = db.connect(path)
            self.addCleanup(second.close)
            self.assertEqual(len(Repo(second).active_assignments()), 1)

    def test_close_is_idempotent(self):
        conn = db.connect(":memory:")
        db.close(conn)
        db.close(conn)


if __name__ == "__main__":
    unittest.main()


class TestBusy(EngineFixture):
    """"I'm busy until X" — a decline and a mute in one reply."""

    async def test_it_silences_nudge_until_the_named_time(self):
        self.add("A")
        await self.engine.tick(dt(16, 10))
        reply = await self.engine.handle(parse("im actually busy until friday"), dt(16, 12))
        self.assertIn("until fri", reply.lower())

        self.repo.kv_set_time("last_offer_at", dt(1))
        await self.engine.tick(dt(16, 30))
        self.assertEqual(len(self.sent), 1)          # nothing further

    async def test_it_wears_off(self):
        self.add("A")
        await self.engine.handle(parse("busy for 2h"), dt(16))
        await self.engine.tick(dt(16, 30))
        self.assertEqual(self.sent, [])
        await self.engine.tick(dt(18, 30))           # after the two hours
        self.assertTrue(self.sent)

    async def test_being_busy_is_not_a_decline_against_the_assignment(self):
        # Otherwise unavailability would push the work down the ranking for
        # reasons that have nothing to do with the work.
        assignment = self.add("A")
        await self.engine.tick(dt(16, 10))
        await self.engine.handle(parse("busy until 9pm"), dt(16, 12))
        self.assertEqual(self.repo.candidates()[0].decline_count, 0)

    async def test_it_closes_the_outstanding_offer(self):
        self.add("A")
        await self.engine.tick(dt(16, 10))
        await self.engine.handle(parse("busy until 9pm"), dt(16, 12))
        self.assertEqual(self.repo.latest_offer().response, "not_now")

    async def test_an_unreadable_duration_falls_back_and_says_so(self):
        self.add("A")
        await self.engine.tick(dt(16, 10))
        reply = await self.engine.handle(parse("busy for ages"), dt(16, 12))
        self.assertIn("Didn't catch", reply)
        mute = self.repo.kv_time("mute_until")
        self.assertAlmostEqual(
            (mute - dt(16, 12)).total_seconds() / 3600, config.DEFAULT_BUSY_HOURS, places=1
        )

    async def test_unmute_cancels_it(self):
        self.add("A")
        await self.engine.handle(parse("busy until friday"), dt(16))
        await self.engine.handle(parse("unmute"), dt(16, 5))
        await self.engine.tick(dt(16, 30))
        self.assertTrue(self.sent)


class TestWhenParsing(unittest.TestCase):
    def test_weekdays(self):
        now = dt(16)  # Tuesday noon local
        friday = tu.parse_when("until friday", now=now)
        self.assertEqual(tu.to_local(friday).strftime("%a %H:%M"), "Fri 08:00")
        self.assertEqual(
            tu.to_local(tu.parse_when("fri 2pm", now=now)).strftime("%a %H:%M"),
            "Fri 14:00",
        )

    def test_a_weekday_that_is_today_rolls_to_next_week(self):
        now = dt(16)  # Tuesday
        result = tu.parse_when("tuesday", now=now)
        self.assertGreater(result, now)

    def test_rest_of_the_day(self):
        result = tu.parse_when("rest of the day", now=dt(16))
        self.assertEqual(tu.to_local(result).strftime("%a %H:%M"), "Wed 08:00")

    def test_leading_prepositions_are_ignored(self):
        now = dt(16)
        self.assertEqual(tu.parse_when("for 2h", now=now), tu.parse_when("2h", now=now))
        self.assertEqual(
            tu.parse_when("until tomorrow", now=now), tu.parse_when("tomorrow", now=now)
        )


class TestFinishingTheLastStretch(unittest.TestCase):
    """The tail of an assignment has to stay reachable."""

    def setUp(self):
        self.repo = Repo(db.connect(":memory:"))
        self.addCleanup(self.repo.conn.close)
        self.now = dt(16)

    def rank(self, gap_min: float):
        return scoring.rank(
            self.repo.candidates(),
            now=self.now,
            usable_gap_min=gap_min,
            continuity_assignment_id=None,
            last_decline_lookup=lambda _i: None,
        )

    def test_a_nearly_finished_assignment_is_still_offered(self):
        self.repo.create_assignment(
            "Nearly done", expected_min=120, percent_done=96,
            due_at=self.now + timedelta(days=2),
        )
        self.assertLess(self.repo.candidates()[0].remaining_min, 5)
        self.assertEqual(len(self.rank(115)), 1)

    def test_the_session_asked_for_is_just_what_is_left(self):
        self.repo.create_assignment(
            "Nearly done", expected_min=120, percent_done=96,
            due_at=self.now + timedelta(days=2),
        )
        candidate = self.rank(115)[0].candidate
        self.assertEqual(
            scoring.session_minutes(candidate.remaining_min, 115), 4
        )

    def test_a_gap_too_short_for_any_useful_slice_offers_nothing(self):
        self.repo.create_assignment(
            "Big project", expected_min=600, due_at=self.now + timedelta(days=2)
        )
        self.assertEqual(self.rank(5), [])

    def test_a_finished_assignment_is_still_excluded(self):
        self.repo.create_assignment(
            "Done", expected_min=120, percent_done=100,
            due_at=self.now + timedelta(days=2),
        )
        self.assertEqual(self.rank(115), [])
