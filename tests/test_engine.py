"""The tick and the replies, end to end against a fake sheet and calendar."""
from __future__ import annotations

import unittest
from datetime import timedelta

from fakes import FakeGaps, FakeSheets, dt

from nudge import config
from nudge import timeutil as tu
from nudge.engine import capacity, policy
from nudge.engine.gaps import Gap
from nudge.engine.loop import NagEngine
from nudge.io.commands import parse
from nudge.sheet import Sheet
from nudge.store import db
from nudge.store.repo import Repo


class EngineCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = Repo(db.connect(":memory:"))
        self.addCleanup(self.repo.conn.close)
        self.client = FakeSheets()
        self.gaps = FakeGaps(Gap(dt(16), dt(19)))     # free noon-3pm local
        self.sent: list[str] = []
        self.offers: list[str] = []
        self.engine = NagEngine(self.repo, Sheet(self.repo, self.client), self.gaps,
                                self.record)

    async def record(self, text: str, *, offer: bool = False) -> None:
        self.sent.append(text)
        if offer:
            self.offers.append(text)

    def add(self, name, expected="2h", due="2026-09-10", **kwargs):
        self.client.add(name, expected=expected, due=due, sheet_id=f"id{name}", **kwargs)

    async def reply(self, text, now):
        return await self.engine.handle(parse(text), now)


class TestOffering(EngineCase):
    async def test_one_offer_per_tick(self):
        self.add("A")
        self.add("B")
        await self.engine.tick(dt(16, 10))
        self.assertEqual(len(self.offers), 1)

    async def test_the_offer_names_a_session_length(self):
        self.add("A", expected="45m")
        await self.engine.tick(dt(16, 10))
        self.assertIn("45 min", self.offers[0])

    async def test_a_long_assignment_is_boxed_to_one_sitting(self):
        self.add("A", expected="20h", due="2026-10-30")
        await self.engine.tick(dt(16, 10))
        self.assertIn(tu.fmt_duration(config.MAX_SESSION_MINUTES), self.offers[0])

    async def test_cooldown_blocks_the_next_tick(self):
        self.add("A")
        self.add("B")
        await self.engine.tick(dt(16, 10))
        await self.engine.tick(dt(16, 20))
        self.assertEqual(len(self.offers), 1)

    async def test_quiet_hours_block_everything(self):
        self.add("A")
        self.engine.gaps = FakeGaps(Gap(dt(3), dt(6)))
        await self.engine.tick(dt(3, 10))  # 23:10 local
        self.assertEqual(self.offers, [])

    async def test_no_gap_no_offer(self):
        self.add("A")
        self.engine.gaps = FakeGaps(None)
        await self.engine.tick(dt(16, 10))
        self.assertEqual(self.offers, [])

    async def test_a_finished_assignment_is_not_offered(self):
        self.add("A", percent="100")
        await self.engine.tick(dt(16, 10))
        self.assertEqual(self.offers, [])

    async def test_a_big_distant_assignment_outranks_a_small_near_one(self):
        self.add("10h project", expected="10h", due="2026-09-12")
        self.add("1h reading", expected="1h", due="2026-09-10")
        await self.engine.tick(dt(16, 10))
        self.assertIn("10h project", self.offers[0])

    async def test_daily_budget_is_enforced(self):
        for i in range(config.MAX_OFFERS_PER_DAY + 2):
            self.add(f"A{i}")
        self.engine.gaps = FakeGaps(Gap(dt(12), dt(23, 59)))
        now = dt(13)
        for _ in range(config.MAX_OFFERS_PER_DAY + 2):
            await self.engine.tick(now)
            now += timedelta(minutes=config.GLOBAL_COOLDOWN + 1)
        self.assertEqual(len(self.offers), config.MAX_OFFERS_PER_DAY)

    async def test_an_unreachable_sheet_skips_the_tick(self):
        self.add("A")

        def broken(*args, **kwargs):
            raise RuntimeError("503 from Google")

        self.client.fetch_grid = broken
        with self.assertLogs("nudge.engine.loop", level="ERROR"):
            await self.engine.tick(dt(16, 10))
        self.assertEqual(self.sent, [])

    async def test_a_capacity_warning_is_sent_once_a_day(self):
        self.add("A", expected="20h", due="2026-09-09")
        await self.engine.tick(dt(16, 10))
        await self.engine.tick(dt(17, 30))
        self.assertEqual(len([m for m in self.sent if "no longer fits" in m]), 1)


class TestProgress(EngineCase):
    async def test_done_on_the_whole_thing_sets_100(self):
        self.add("A", expected="1h")
        await self.engine.tick(dt(16, 10))
        reply = await self.reply("done", dt(16, 20))
        self.assertIn("100%", reply)
        self.assertEqual(self.client.row("A")["% done"], "100")

    async def test_done_on_a_slice_logs_the_slice(self):
        self.add("A", expected="4h")
        await self.engine.tick(dt(16, 10))  # asks for 2h
        reply = await self.reply("done", dt(18))
        self.assertIn("50%", reply)
        self.assertEqual(self.client.row("A")["% done"], "50")

    async def test_logging_minutes_advances_the_percentage(self):
        self.add("A", expected="2h")
        await self.engine.tick(dt(16, 10))
        reply = await self.reply("did 30", dt(16, 45))
        self.assertIn("25%", reply)
        self.assertEqual(self.client.row("A")["% done"], "25")

    async def test_logging_adds_to_what_the_sheet_already_says(self):
        self.add("A", expected="2h", percent="50")
        await self.engine.tick(dt(16, 10))
        await self.reply("did 30", dt(16, 45))
        self.assertEqual(self.client.row("A")["% done"], "75")

    async def test_logging_past_the_end_completes_it(self):
        self.add("A", expected="1h")
        await self.engine.tick(dt(16, 10))
        await self.reply("did 90", dt(16, 45))
        self.assertEqual(self.client.row("A")["% done"], "100")

    async def test_setting_a_percentage_directly(self):
        self.add("A")
        await self.engine.tick(dt(16, 10))
        await self.reply("60%", dt(16, 45))
        self.assertEqual(self.client.row("A")["% done"], "60")

    async def test_time_cannot_be_logged_without_an_expected_length(self):
        self.add("A", expected="")
        await self.engine.tick(dt(16, 10))
        self.assertIn("expected length", await self.reply("did 30", dt(16, 45)))

    async def test_answering_twice_does_not_count_twice(self):
        self.add("A", expected="4h")
        await self.engine.tick(dt(16, 10))
        await self.reply("did 60", dt(16, 45))
        self.assertIn("No offer outstanding", await self.reply("did 60", dt(16, 46)))
        self.assertEqual(self.client.row("A")["% done"], "25")

    async def test_progress_is_in_the_offers_log(self):
        self.add("A")
        await self.engine.tick(dt(16, 10))
        await self.reply("did 30", dt(16, 45))
        row = self.repo.recent_offers(1)[0]
        self.assertEqual((row["response"], row["minutes_logged"]), ("progress", 30))

    async def test_progress_resets_the_decline_count(self):
        self.add("A")
        self.repo.bump_decline("idA")
        await self.engine.tick(dt(16, 10))
        await self.reply("did 20", dt(16, 45))
        self.assertEqual(self.repo.state("idA").decline_count, 0)

    async def test_a_row_deleted_meanwhile_is_reported(self):
        self.add("A")
        await self.engine.tick(dt(16, 10))
        del self.client.grid[1]
        self.assertIn("isn't in the sheet", await self.reply("done", dt(16, 20)))


class TestDeclining(EngineCase):
    async def test_not_now_silences_the_rest_of_the_gap(self):
        self.add("A")
        self.add("B")
        await self.engine.tick(dt(16, 10))
        await self.reply("not now", dt(16, 12))
        await self.engine.tick(dt(17, 30))   # past the cooldown, same gap
        self.assertEqual(len(self.offers), 1)

    async def test_something_else_offers_the_next_one(self):
        self.add("A")
        self.add("B")
        await self.engine.tick(dt(16, 10))
        await self.reply("something else", dt(16, 12))
        self.assertEqual(len(self.offers), 2)
        self.assertNotEqual(self.offers[0], self.offers[1])

    async def test_snooze_takes_it_out_of_the_running(self):
        self.add("A")
        await self.engine.tick(dt(16, 10))
        await self.reply("snooze 2h", dt(16, 12))
        ranking = self.engine.rank(self.engine.sheet.read(dt(16, 30)), self.gaps.gap, dt(16, 30))
        self.assertEqual(ranking, [])

    async def test_a_bare_snooze_means_tomorrow_morning(self):
        self.add("A")
        await self.engine.tick(dt(16, 10))
        reply = await self.reply("snooze", dt(16, 12))
        self.assertIn("wed 8:00am", reply)

    async def test_repeated_passes_set_it_aside_until_tomorrow(self):
        self.add("A")
        for _ in range(config.MAX_DECLINES):
            self.engine._offered_this_gap.clear()
            await self.engine.offer_best(self.engine.sheet.read(dt(16, 10)),
                                         self.gaps.gap, dt(16, 10))
            reply = await self.reply("not now", dt(16, 11))
        self.assertIn("leave it alone", reply)
        snooze = self.repo.state("idA").snooze_until
        self.assertEqual(tu.fmt_when(snooze), "wed 8:00am")


class TestBusy(EngineCase):
    async def test_it_silences_nudge_until_the_named_time(self):
        self.add("A")
        await self.engine.tick(dt(16, 10))
        reply = await self.reply("im actually busy until friday", dt(16, 12))
        self.assertIn("fri", reply)
        await self.engine.tick(dt(17, 30))
        self.assertEqual(len(self.offers), 1)

    async def test_it_wears_off(self):
        self.add("A")
        await self.reply("busy for 1h", dt(16))
        await self.engine.tick(dt(16, 30))
        self.assertEqual(self.offers, [])
        await self.engine.tick(dt(17, 30))
        self.assertEqual(len(self.offers), 1)

    async def test_busy_off_cancels_it(self):
        self.add("A")
        await self.reply("busy until friday", dt(16))
        await self.reply("busy off", dt(16, 5))
        await self.engine.tick(dt(16, 30))
        self.assertEqual(len(self.offers), 1)

    async def test_it_is_not_a_decline_against_the_assignment(self):
        self.add("A")
        await self.engine.tick(dt(16, 10))
        await self.reply("busy until 9pm", dt(16, 12))
        self.assertEqual(self.repo.state("idA").decline_count, 0)

    async def test_an_unreadable_duration_falls_back_and_says_so(self):
        await self.engine.tick(dt(16, 10))
        reply = await self.reply("busy for ages", dt(16, 12))
        self.assertIn("Didn't catch", reply)
        until = self.repo.kv_time(policy.BUSY_UNTIL)
        self.assertEqual(until, dt(16, 12) + timedelta(hours=config.DEFAULT_BUSY_HOURS))


class TestStatus(EngineCase):
    async def test_status_shows_percentages_and_whats_left(self):
        self.add("A", expected="2h", percent="25")
        self.add("B", percent="100")
        reply = await self.reply("status", dt(16))
        self.assertIn("25%", reply)
        self.assertIn("1h30 left", reply)
        self.assertNotIn("· B", reply)

    async def test_measured_availability_reaches_the_ranking(self):
        self.add("A", expected="90m", due="2026-09-11")
        capacity.write_snapshot(self.repo, dt(16), {"idA": 60.0})
        ranking = self.engine.rank(self.engine.sheet.read(dt(16)), self.gaps.gap, dt(16))
        # 90 minutes of work against 60 * 0.6 = 36 usable: it does not fit.
        self.assertAlmostEqual(ranking[0].pressure, 90 / 36)


if __name__ == "__main__":
    unittest.main()
