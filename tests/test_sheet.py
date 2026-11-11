"""The sheet: reading rows, writing progress, rolling repeats. No network."""
from __future__ import annotations

import unittest

from fakes import FakeSheets, dt

from nudge import recurrence
from nudge import timeutil as tu
from nudge.connectors import gsheets
from nudge.sheet import Sheet, remaining_minutes
from nudge.store import db
from nudge.store.repo import Repo


class SheetCase(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(db.connect(":memory:"))
        self.addCleanup(self.repo.conn.close)
        self.client = FakeSheets()
        self.sheet = Sheet(self.repo, self.client)
        self.now = dt(16)

    def read(self, now=None):
        return {a.name: a for a in self.sheet.read(now or self.now)}


class TestReading(SheetCase):
    def test_a_row_becomes_an_assignment(self):
        self.client.add("HW1", due="2026-09-17", expected="8h", percent="25", sheet_id="ida")
        a = self.read()["HW1"]
        self.assertEqual(a.course, "CS 4262")
        self.assertEqual(a.expected_min, 480)
        self.assertEqual(a.percent_done, 25)
        self.assertEqual(a.remaining_min, 360)
        self.assertEqual(a.label, "CS 4262 · HW1")

    def test_a_blank_id_is_filled_in_and_written_back(self):
        self.client.add("HW1")
        a = self.read()["HW1"]
        self.assertEqual(self.client.row("HW1")["ID"], a.sheet_id)
        self.assertEqual(self.read()["HW1"].sheet_id, a.sheet_id)  # stable

    def test_a_copied_row_gets_its_own_id(self):
        self.client.add("HW1", sheet_id="ida")
        self.client.add("HW2", sheet_id="ida")
        rows = self.read()
        self.assertEqual(rows["HW1"].sheet_id, "ida")
        self.assertNotEqual(rows["HW2"].sheet_id, "ida")

    def test_blank_rows_are_ignored(self):
        self.client.grid.append(["", "", "", ""])
        self.client.add("HW1", sheet_id="ida")
        self.assertEqual(list(self.read()), ["HW1"])

    def test_reordered_and_extra_columns_still_parse(self):
        self.client.grid = [
            ["Class", "Notes", "Assignment", "% done", "Due date", "Expected length",
             "Repeats", "Not before", "ID"],
            ["CS 4262", "ask TA", "HW1", "50", "2026-09-17", "2h", "", "", "ida"],
        ]
        a = self.read()["HW1"]
        self.assertEqual((a.course, a.percent_done, a.expected_min), ("CS 4262", 50, 120))

    def test_missing_columns_are_appended_without_shuffling(self):
        self.client.grid = [
            ["Class", "Assignment", "Due date", "Expected length", "% done", "ID"],
            ["CS 4262", "HW1", "2026-09-17", "8h", "0", "ida"],
        ]
        self.assertEqual(self.read()["HW1"].expected_min, 480)
        self.assertEqual(self.client.grid[0][:2], ["Class", "Assignment"])
        self.assertEqual(self.client.grid[0][-2:], ["Repeats", "Not before"])

    def test_an_unreadable_header_is_never_stamped_over(self):
        self.client.grid = [["what", "is", "this"], ["some", "stray", "data"]]
        with self.assertLogs("nudge.sheet", level="ERROR"):
            self.assertEqual(self.sheet.read(self.now), [])
        self.assertEqual(self.client.grid[0], ["what", "is", "this"])

    def test_the_spreadsheet_is_created_once(self):
        self.sheet.read(self.now)
        self.sheet.read(self.now)
        self.assertEqual(self.client.created, 1)


class TestWriting(SheetCase):
    def test_progress_is_written_to_the_right_row(self):
        self.client.add("HW1", sheet_id="ida")
        self.client.add("HW2", sheet_id="idb")
        self.assertTrue(self.sheet.write_percent("idb", 40))
        self.assertEqual(self.client.row("HW2")["% done"], "40")
        self.assertEqual(self.client.row("HW1")["% done"], "")

    def test_a_row_that_moved_is_still_found(self):
        self.client.add("HW1", sheet_id="ida")
        self.client.add("HW2", sheet_id="idb")
        self.client.grid[1], self.client.grid[2] = self.client.grid[2], self.client.grid[1]
        self.sheet.write_percent("ida", 75)
        self.assertEqual(self.client.row("HW1")["% done"], "75")

    def test_a_deleted_row_is_reported_not_recreated(self):
        with self.assertLogs("nudge.sheet", level="WARNING"):
            self.assertFalse(self.sheet.write_percent("gone", 50))
        self.assertEqual(len(self.client.grid), 1)


class TestRepeats(SheetCase):
    def test_rules_parse_in_several_shapes(self):
        self.assertEqual(recurrence.parse_rule("weekly:fri"), {4})
        self.assertEqual(recurrence.parse_rule("sun,tue"), {6, 1})
        self.assertEqual(recurrence.parse_rule("every Monday"), {0})
        self.assertIsNone(recurrence.parse_rule(""))

    def test_a_blank_due_date_gets_the_next_occurrence(self):
        self.client.add("RFTH", repeat="weekly:fri", expected="1h", sheet_id="r1")
        a = self.read()["RFTH"]
        self.assertEqual(tu.to_local(a.due_at).strftime("%a %Y-%m-%d"), "Fri 2026-09-11")
        self.assertEqual(self.client.row("RFTH")["Due date"], "2026-09-11")

    def test_a_finished_occurrence_rolls_to_the_next_week(self):
        self.client.add("RFTH", repeat="weekly:fri", due="2026-09-11", percent="100",
                        sheet_id="r1")
        a = self.read()["RFTH"]
        self.assertEqual(gsheets.format_date(a.due_at), "2026-09-18")
        self.assertEqual(a.percent_done, 0)
        self.assertEqual(self.client.row("RFTH")["% done"], "0")

    def test_a_missed_occurrence_rolls_forward_too(self):
        self.client.add("RFTH", repeat="weekly:fri", due="2026-09-04", sheet_id="r1")
        self.assertEqual(gsheets.format_date(self.read()["RFTH"].due_at), "2026-09-11")

    def test_an_unfinished_future_occurrence_is_left_alone(self):
        self.client.add("RFTH", repeat="weekly:fri", due="2026-09-11", percent="50",
                        sheet_id="r1")
        a = self.read()["RFTH"]
        self.assertEqual((gsheets.format_date(a.due_at), a.percent_done), ("2026-09-11", 50))

    def test_rolling_clears_snoozes_and_declines(self):
        self.repo.set_snooze("r1", dt(16, day=20))
        self.client.add("RFTH", repeat="weekly:fri", due="2026-09-04", sheet_id="r1")
        self.read()
        self.assertIsNone(self.repo.state("r1").snooze_until)


class TestNotBefore(SheetCase):
    def test_rules_resolve_against_the_due_date(self):
        friday = gsheets.parse_date("2026-09-11")
        monday = gsheets.parse_date("2026-09-14")
        opens = recurrence.resolve_not_before(recurrence.parse_not_before("fri 10am"), friday)
        self.assertEqual(tu.to_local(opens).strftime("%a %H:%M"), "Fri 10:00")
        # Released Friday 5pm but due Monday: it opens the Friday *before*.
        opens = recurrence.resolve_not_before(recurrence.parse_not_before("fri 5pm"), monday)
        self.assertEqual(tu.to_local(opens).strftime("%a %d %H:%M"), "Fri 11 17:00")

    def test_an_unreadable_rule_is_ignored_rather_than_guessed(self):
        self.assertIsNone(recurrence.parse_not_before("whenever"))
        self.assertIsNone(recurrence.parse_not_before(""))

    def test_the_opening_is_read_from_the_sheet(self):
        self.client.add("RFTH", due="2026-09-11", not_before="fri 10am", sheet_id="r1")
        opens = self.read()["RFTH"].available_from
        self.assertEqual(tu.to_local(opens).strftime("%a %H:%M"), "Fri 10:00")

    def test_rolling_forward_moves_the_opening_too(self):
        self.client.add("RFTH", repeat="weekly:fri", due="2026-09-04", percent="100",
                        not_before="fri 10am", sheet_id="r1")
        a = self.read()["RFTH"]
        self.assertEqual(tu.to_local(a.available_from).strftime("%m-%d %H:%M"), "09-11 10:00")


class TestCellFormats(unittest.TestCase):
    def test_durations(self):
        for text, minutes in [("3h", 180), ("90m", 90), ("1h30", 90), ("2.5h", 150)]:
            self.assertEqual(gsheets.parse_duration(text), minutes)

    def test_a_bare_number_means_hours(self):
        self.assertEqual(gsheets.parse_duration("3"), 180)

    def test_unreadable_values_do_not_raise(self):
        with self.assertLogs("nudge.connectors.gsheets", level="WARNING"):
            self.assertIsNone(gsheets.parse_duration("ages"))
        with self.assertLogs("nudge.connectors.gsheets", level="WARNING"):
            self.assertEqual(gsheets.parse_percent("nearly"), 0.0)

    def test_a_fraction_is_read_as_a_percentage(self):
        self.assertEqual(gsheets.parse_percent("0.45"), 45)

    def test_dates_accept_several_shapes(self):
        for text in ("2026-09-17", "9/17/2026", "Sep 17"):
            parsed = gsheets.parse_date(text, now=dt(16))
            self.assertEqual(gsheets.format_date(parsed), "2026-09-17")

    def test_a_due_date_is_the_end_of_the_local_day(self):
        self.assertEqual(tu.to_local(gsheets.parse_date("2026-09-17")).hour, 23)

    def test_ids_never_look_numeric(self):
        # Sheets reads "202e65" as scientific notation and eats leading zeros.
        for _ in range(200):
            generated = gsheets.new_sheet_id()
            self.assertTrue(generated[0].isalpha(), generated)

    def test_remaining_tracks_percent(self):
        self.assertEqual(remaining_minutes(180, 50), 90)
        self.assertEqual(remaining_minutes(180, 100), 0)


if __name__ == "__main__":
    unittest.main()
