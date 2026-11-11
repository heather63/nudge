"""Small pure pieces that are easy to get subtly wrong."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from fakes import dt

from nudge import timeutil as tu
from nudge.engine import policy, scoring
from nudge.io.commands import parse, parse_worked_minutes
from nudge.sheet import Assignment
from nudge.store import db
from nudge.store.repo import Repo, State


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
        self.assertEqual(parse("1h").name, "log_minutes")

    def test_not_done_yet_is_not_done(self):
        self.assertEqual(parse("not done yet").name, "not_now")

    def test_common_words_do_not_trigger_commands(self):
        # The old parser read these as "busy" / "something else" / "help".
        for text in ("I'm out of ideas", "next week maybe", "the other one?"):
            self.assertIsNone(parse(text), text)

    def test_busy_wins_over_not_now(self):
        self.assertEqual(parse("not now, busy until 5pm").name, "busy")

    def test_worked_minutes_reads_bare_numbers_as_minutes(self):
        self.assertEqual(parse_worked_minutes("30"), 30)
        self.assertEqual(parse_worked_minutes("45 min"), 45)
        self.assertEqual(parse_worked_minutes("2h"), 120)
        self.assertIsNone(parse_worked_minutes("later"))


class TestWhen(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(tu.parse_when("90m", now=dt(12)), dt(13, 30))
        self.assertIsNone(tu.parse_when("whenever", now=dt(12)))

    def test_weekdays(self):
        now = dt(16)  # Tuesday noon local
        self.assertEqual(tu.fmt_when(tu.parse_when("until friday", now=now)), "fri 8:00am")
        self.assertEqual(tu.fmt_when(tu.parse_when("fri 2pm", now=now)), "fri 2:00pm")
        self.assertGreater(tu.parse_when("tuesday", now=now), now)

    def test_tomorrow_is_when_quiet_hours_end(self):
        self.assertEqual(tu.fmt_when(tu.parse_when("tomorrow", now=dt(16))), "wed 8:00am")


class TestPolicy(unittest.TestCase):
    def test_quiet_hours_wrap_midnight(self):
        self.assertTrue(policy.in_quiet_hours(dt(4)))      # 00:00 local
        self.assertFalse(policy.in_quiet_hours(dt(16)))    # 12:00 local
        self.assertTrue(policy.in_quiet_hours(dt(3, 30)))  # 23:30 local


class TestScoring(unittest.TestCase):
    now = dt(16)

    def rank(self, *assignments, gap=115.0, **kwargs):
        return scoring.rank(list(assignments), now=self.now, usable_gap_min=gap, **kwargs)

    def a(self, name="A", expected=120, percent=0.0, days=2, **kwargs):
        return Assignment(sheet_id=name, name=name, expected_min=expected,
                          percent_done=percent, due_at=self.now + timedelta(days=days),
                          **kwargs)

    def test_fit_rewards_being_able_to_finish(self):
        self.assertEqual(scoring.fit(30, 120), 1.0)
        self.assertLess(scoring.fit(1200, 120), scoring.fit(240, 120))

    def test_session_is_the_smaller_of_gap_and_remaining(self):
        self.assertEqual(scoring.session_minutes(240, 120), 120)
        self.assertEqual(scoring.session_minutes(25, 120), 25)

    def test_equal_pressure_goes_to_what_you_can_finish(self):
        small, big = self.a("small", expected=60), self.a("big", expected=600, days=20)
        # Same deadline pressure is hard to arrange; no deadline at all is easy.
        small.due_at = big.due_at = None
        self.assertEqual(self.rank(big, small)[0].assignment.name, "small")

    def test_a_nearly_finished_assignment_is_still_offered(self):
        ranking = self.rank(self.a(percent=96), gap=5)
        self.assertEqual(len(ranking), 1)
        self.assertEqual(scoring.session_minutes(ranking[0].assignment.remaining_min, 5), 4)

    def test_a_gap_too_short_for_a_useful_slice_offers_nothing(self):
        self.assertEqual(self.rank(self.a(expected=600), gap=5), [])

    def test_finished_snoozed_and_unopened_work_is_excluded(self):
        done = self.a("done", percent=100)
        snoozed = self.a("snoozed")
        unopened = self.a("unopened", available_from=self.now + timedelta(hours=3))
        states = {"snoozed": State(snooze_until=self.now + timedelta(hours=1))}
        self.assertEqual(self.rank(done, snoozed, unopened, states=states), [])


class TestDatabase(unittest.TestCase):
    def test_data_survives_a_checkpoint_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nudge.db"
            first = db.connect(path)
            Repo(first).set_snooze("ida", dt(16))
            db.close(first)
            self.assertFalse((Path(tmp) / "nudge.db-wal").exists())
            second = db.connect(path)
            self.addCleanup(second.close)
            self.assertEqual(Repo(second).state("ida").snooze_until, dt(16))

    def test_close_is_idempotent(self):
        conn = db.connect(":memory:")
        db.close(conn)
        db.close(conn)

    def test_an_old_mirror_database_keeps_its_spreadsheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nudge.db"
            old = sqlite3.connect(path)
            old.executescript(
                "CREATE TABLE assignments (id INTEGER PRIMARY KEY, name TEXT);"
                "CREATE TABLE offers (id INTEGER PRIMARY KEY, assignment_id INTEGER);"
                "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "INSERT INTO kv VALUES ('spreadsheet_id', 'abc'), ('tasks_sync_cursor', 'x');"
            )
            old.close()
            with self.assertLogs("nudge.store.db", level="WARNING"):
                conn = db.connect(path)
            self.addCleanup(conn.close)
            repo = Repo(conn)
            self.assertEqual(repo.kv_get("spreadsheet_id"), "abc")
            self.assertIsNone(repo.kv_get("tasks_sync_cursor"))
            repo.record_offer("ida", "A", dt(16), dt(17), 30, dt(16))  # new schema works


if __name__ == "__main__":
    unittest.main()
