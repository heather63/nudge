"""Sheet <-> SQLite <-> Google Tasks, against fake clients (no network)."""
from __future__ import annotations

import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

from nudge import config
from nudge import timeutil as tu
from nudge.connectors import gsheets
from nudge.connectors.gsheets import HEADERS, SheetsClient
from nudge.mirror import Mirror, guess_kind
from nudge.store import db
from nudge.store.repo import Repo

UTC = timezone.utc


class FakeSheets(SheetsClient):
    """Overrides only the transport, so the real parsing is under test."""

    def __init__(self, grid: list[list[str]] | None = None) -> None:
        super().__init__(service=object())
        self.grid = grid if grid is not None else [list(HEADERS)]
        self.created = False

    def create(self, title: str = "") -> str:
        self.created = True
        return "sheet1"

    def fetch_grid(self, spreadsheet_id: str) -> list[list[str]]:
        return [list(row) for row in self.grid]

    def _cell(self, ref: str) -> tuple[int, int]:
        letters = "".join(c for c in ref if c.isalpha())
        number = int("".join(c for c in ref if c.isdigit()))
        column = 0
        for char in letters:
            column = column * 26 + (ord(char.upper()) - 64)
        return number - 1, column - 1

    def update_cells(self, spreadsheet_id, updates):
        for ref, value in updates:
            row, column = self._cell(ref)
            while len(self.grid) <= row:
                self.grid.append([])
            while len(self.grid[row]) <= column:
                self.grid[row].append("")
            self.grid[row][column] = str(value)

    def append(self, spreadsheet_id, rows):
        for row in rows:
            self.grid.append([str(v) for v in row])

    def write_values(self, spreadsheet_id, anchor, values):
        self.grid = [list(v) for v in values]

    # test helper
    def row_for(self, name: str) -> dict[str, str]:
        header = self.grid[0]
        for row in self.grid[1:]:
            if row and row[0] == name:
                return {h: (row[i] if i < len(row) else "") for i, h in enumerate(header)}
        raise AssertionError(f"no row named {name!r} in {self.grid}")


class FakeTasks:
    def __init__(self, tasklists: dict[str, str] | None = None) -> None:
        self.tasks: dict[str, dict] = {}
        self.tasklists = tasklists or {"l1": "CS 4262"}
        self._n = 0

    def _stamp(self) -> str:
        return tu.iso(tu.now_utc())

    def list_tasklists(self):
        return [{"id": i, "title": t} for i, t in self.tasklists.items()]

    def ensure_tasklist(self, title):
        for list_id, existing in self.tasklists.items():
            if existing == title:
                return list_id
        self._n += 1
        list_id = f"list{self._n}"
        self.tasklists[list_id] = title
        return list_id

    def list_tasks(self, tasklist_id, updated_min=None):
        items = [t for t in self.tasks.values() if t["tasklist"] == tasklist_id]
        if updated_min:
            items = [t for t in items if t["updated"] >= updated_min]
        return [dict(t) for t in items]

    def get(self, tasklist_id, task_id):
        task = self.tasks.get(task_id)
        return dict(task) if task else None

    def insert(self, tasklist_id, title, *, notes=None, due=None, parent=None):
        self._n += 1
        task_id = f"t{self._n}"
        task = {
            "id": task_id, "title": title, "status": "needsAction",
            "updated": self._stamp(), "tasklist": tasklist_id,
        }
        if notes:
            task["notes"] = notes
        if due:
            task["due"] = due
        self.tasks[task_id] = task
        return dict(task)

    def patch(self, tasklist_id, task_id, body):
        task = self.tasks[task_id]
        for key, value in body.items():
            if value is None:
                task.pop(key, None)
            else:
                task[key] = value
        task["updated"] = self._stamp()
        return dict(task)

    def delete(self, tasklist_id, task_id):
        task = self.tasks.get(task_id)
        if task:
            task["deleted"] = True
            task["updated"] = self._stamp()

    def task_named(self, title: str) -> dict:
        for task in self.tasks.values():
            if task["title"] == title:
                return task
        raise AssertionError(f"no task named {title!r}")


class MirrorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repo(db.connect(":memory:"))
        self.addCleanup(self.repo.conn.close)
        self.sheets = FakeSheets()
        self.tasks = FakeTasks()
        self.mirror = Mirror(self.repo, self.sheets, self.tasks)

    def add_sheet_row(self, name, course="CS 4262", due="", kind="", expected="",
                      percent="", repeat="", not_before="", files="", sheet_id=""):
        """Placed by canonical column order, so adding a column can't skew it."""
        values = {
            "name": name, "course": course, "due": due, "kind": kind,
            "expected": expected, "percent": percent, "repeat": repeat,
            "not_before": not_before, "files": files, "sheet_id": sheet_id,
        }
        self.sheets.grid.append([values[key] for key in gsheets.CANONICAL])


class TestSheetToLocal(MirrorCase):
    def test_a_new_row_becomes_an_assignment(self):
        self.add_sheet_row("HW1", due="2026-09-17", kind="homework", expected="3h")
        report = self.mirror.sync()

        self.assertEqual(report.new_from_sheet, ["CS 4262 · HW1"])
        candidate = self.repo.candidates()[0]
        self.assertEqual(candidate.name, "HW1")
        self.assertEqual(candidate.course, "CS 4262")
        self.assertEqual(candidate.expected_min, 180)
        self.assertEqual(tu.to_local(candidate.due_at).date().isoformat(), "2026-09-17")

    def test_a_blank_id_is_filled_in_and_written_back(self):
        self.add_sheet_row("HW1")
        self.mirror.sync()
        written = self.sheets.row_for("HW1")["ID"]
        self.assertTrue(written)
        self.assertEqual(self.repo.candidates()[0].sheet_id, written)

    def test_an_edit_in_the_sheet_wins(self):
        self.add_sheet_row("HW1", expected="2h", sheet_id="abc")
        self.mirror.sync()
        self.sheets.grid[1][gsheets.CANONICAL.index("expected")] = "5h"
        self.mirror.sync()
        self.assertEqual(self.repo.candidates()[0].expected_min, 300)

    def test_a_hundred_percent_row_is_not_offered(self):
        self.add_sheet_row("HW1", expected="2h", percent="100", sheet_id="abc")
        self.mirror.sync()
        self.assertEqual(self.repo.candidates(), [])
        self.assertEqual(
            self.repo.assignment_by_sheet_id("abc")["status"], "done"
        )

    def test_a_deleted_row_drops_the_assignment(self):
        self.add_sheet_row("HW1", sheet_id="abc")
        self.add_sheet_row("Quiz 2", sheet_id="def")
        self.mirror.sync()
        del self.sheets.grid[1]
        report = self.mirror.sync()
        self.assertEqual(report.dropped, ["CS 4262 · HW1"])
        self.assertEqual([c.name for c in self.repo.candidates()], ["Quiz 2"])

    def test_blank_rows_are_ignored(self):
        self.sheets.grid.append([""] * len(gsheets.CANONICAL))
        self.add_sheet_row("HW1")
        self.mirror.sync()
        self.assertEqual(len(self.repo.candidates()), 1)

    def test_reordered_columns_still_parse(self):
        self.sheets.grid = [["ID", "Class", "Assignment", "% done", "Expected length"]]
        self.sheets.grid.append(["abc", "CX 3803", "Sprint 2", "50", "6h"])
        self.mirror.sync()
        candidate = self.repo.candidates()[0]
        self.assertEqual(candidate.name, "Sprint 2")
        self.assertEqual(candidate.percent_done, 50)
        self.assertEqual(candidate.remaining_min, 180)


class TestLocalToSheet(MirrorCase):
    def test_progress_is_written_back(self):
        self.add_sheet_row("HW1", expected="2h", sheet_id="abc")
        self.mirror.sync()
        assignment = self.repo.assignment_by_sheet_id("abc")
        self.repo.add_progress(int(assignment["id"]), 30)

        report = self.mirror.sync()
        self.assertEqual(report.pushed_to_sheet, 1)
        self.assertEqual(self.sheets.row_for("HW1")["% done"], "25")

    def test_an_unpushed_local_change_is_not_clobbered_by_the_sheet(self):
        self.add_sheet_row("HW1", expected="2h", percent="0", sheet_id="abc")
        self.mirror.sync()
        assignment = int(self.repo.assignment_by_sheet_id("abc")["id"])
        self.repo.set_percent(assignment, 60)   # dirty, not yet pushed

        self.mirror.sync()
        self.assertEqual(self.repo.get_assignment(assignment)["percent_done"], 60)
        self.assertEqual(self.sheets.row_for("HW1")["% done"], "60")

    def test_an_assignment_created_locally_is_appended(self):
        self.mirror.sync()
        self.repo.create_assignment("Lab 1", course="PHYS 2211", expected_min=90)
        self.mirror.sync()
        row = self.sheets.row_for("Lab 1")
        self.assertEqual(row["Class"], "PHYS 2211")
        self.assertEqual(row["Expected length"], "1h30")
        self.assertTrue(row["ID"])

    def test_the_spreadsheet_is_created_once(self):
        self.mirror.sync()
        self.assertTrue(self.sheets.created)
        self.sheets.created = False
        self.mirror.sync()
        self.assertFalse(self.sheets.created)


class TestTasksMirror(MirrorCase):
    def setUp(self):
        super().setUp()
        self.add_sheet_row("HW1", expected="2h", due="2026-09-17", sheet_id="abc")

    def test_a_task_is_created_in_the_class_list(self):
        self.mirror.sync()
        task = self.tasks.task_named("HW1")
        self.assertEqual(task["tasklist"], "l1")
        self.assertEqual(task["due"][:10], "2026-09-17")

    def test_a_class_with_no_list_gets_one(self):
        self.add_sheet_row("Lab 1", course="PHYS 2211")
        self.mirror.sync()
        self.assertIn("PHYS 2211", self.tasks.tasklists.values())

    def test_completing_the_task_flows_back_to_the_sheet(self):
        self.mirror.sync()
        task = self.tasks.task_named("HW1")
        self.tasks.patch("l1", task["id"], {"status": "completed"})

        report = self.mirror.sync()
        self.assertEqual(report.completed_elsewhere, ["CS 4262 · HW1"])
        self.assertEqual(self.sheets.row_for("HW1")["% done"], "100")

    def test_reaching_a_hundred_percent_completes_the_task(self):
        self.mirror.sync()
        assignment = int(self.repo.assignment_by_sheet_id("abc")["id"])
        self.repo.set_percent(assignment, 100)
        self.mirror.sync()
        self.assertEqual(self.tasks.task_named("HW1")["status"], "completed")

    def test_reopening_a_task_asks_rather_than_inventing_a_number(self):
        self.mirror.sync()
        assignment = int(self.repo.assignment_by_sheet_id("abc")["id"])
        self.repo.set_percent(assignment, 100)
        self.mirror.sync()

        task = self.tasks.task_named("HW1")
        self.tasks.patch("l1", task["id"], {"status": "needsAction"})
        report = self.mirror.sync()
        self.assertEqual(report.reopened_elsewhere, ["CS 4262 · HW1"])
        self.assertEqual(self.repo.get_assignment(assignment)["percent_done"], 100)
        self.assertEqual(self.repo.get_assignment(assignment)["status"], "active")

    def test_partial_progress_shows_in_the_task_notes(self):
        self.mirror.sync()
        assignment = int(self.repo.assignment_by_sheet_id("abc")["id"])
        self.repo.set_percent(assignment, 40)
        self.mirror.sync()
        self.assertEqual(self.tasks.task_named("HW1")["notes"], "40% done")

    def test_a_deleted_task_is_recreated_because_the_sheet_rules(self):
        self.mirror.sync()
        task = self.tasks.task_named("HW1")
        self.tasks.delete("l1", task["id"])
        self.mirror.sync()

        live = [
            t for t in self.tasks.tasks.values()
            if t["title"] == "HW1" and not t.get("deleted")
        ]
        self.assertEqual(len(live), 1)

    def test_sync_is_idempotent(self):
        self.mirror.sync()
        before_tasks = len(self.tasks.tasks)
        before_rows = len(self.sheets.grid)
        self.mirror.sync()
        self.mirror.sync()
        self.assertEqual(len(self.tasks.tasks), before_tasks)
        self.assertEqual(len(self.sheets.grid), before_rows)


class TestAdoptingTasks(MirrorCase):
    """A task typed into the Tasks app becomes a sheet row on the next sync."""

    def hand_made(self, title="Update Resume", tasklist="l1", **fields):
        """A task nudge did not create — as if you typed it into the app."""
        task = self.tasks.insert(tasklist, title, **fields)
        return task

    def test_a_hand_made_task_becomes_a_sheet_row(self):
        self.hand_made("Update Resume")
        report = self.mirror.sync()
        self.assertEqual(report.adopted_from_tasks, ["CS 4262 · Update Resume"])
        self.assertIsNotNone(self.sheets.row_for("Update Resume"))

    def test_it_lands_in_the_same_pass_not_the_next_one(self):
        self.hand_made("Update Resume")
        self.mirror.sync()
        row = self.sheets.row_for("Update Resume")
        self.assertEqual(row["Class"], "CS 4262")

    def test_the_due_date_comes_across(self):
        self.hand_made("Post Classroom Location", due="2026-09-13T00:00:00.000Z")
        self.mirror.sync()
        self.assertEqual(
            self.sheets.row_for("Post Classroom Location")["Due date"], "2026-09-13"
        )

    def test_the_type_is_guessed_from_the_title(self):
        self.hand_made("Midterm Exam 3")
        self.mirror.sync()
        self.assertEqual(self.sheets.row_for("Midterm Exam 3")["Type"], "exam")

    def test_the_row_is_linked_to_the_task_it_came_from(self):
        task = self.hand_made("Update Resume")
        self.mirror.sync()
        local = self.repo.assignment_by_gtask(task["id"])
        self.assertIsNotNone(local)
        self.assertEqual(local["name"], "Update Resume")

    def test_it_does_not_push_a_second_copy_of_the_task(self):
        self.hand_made("Update Resume")
        self.mirror.sync()
        live = [t for t in self.tasks.tasks.values()
                if t["title"] == "Update Resume" and not t.get("deleted")]
        self.assertEqual(len(live), 1)

    def test_adoption_is_idempotent(self):
        self.hand_made("Update Resume")
        self.mirror.sync()
        rows, tasks = len(self.sheets.grid), len(self.tasks.tasks)
        self.mirror.sync(full=True)
        self.mirror.sync(full=True)
        self.assertEqual(len(self.sheets.grid), rows)
        self.assertEqual(len(self.tasks.tasks), tasks)

    def test_a_completed_hand_made_task_arrives_finished(self):
        task = self.hand_made("Update Resume")
        self.tasks.patch("l1", task["id"], {"status": "completed"})
        self.mirror.sync()
        self.assertEqual(self.sheets.row_for("Update Resume")["% done"], "100")

    def test_a_subtask_is_not_an_assignment(self):
        parent = self.hand_made("Big Project")
        step = self.tasks.insert("l1", "step one")
        self.tasks.tasks[step["id"]]["parent"] = parent["id"]
        self.mirror.sync()
        self.assertEqual(self.repo.find_assignment("CS 4262", "step one"), None)

    def test_a_blank_title_is_skipped(self):
        self.hand_made("   ")
        report = self.mirror.sync()
        self.assertEqual(report.adopted_from_tasks, [])

    def test_the_catch_all_list_means_no_class(self):
        self.tasks.tasklists["lz"] = config.DEFAULT_TASKLIST
        self.hand_made("Update Resume", tasklist="lz")
        self.mirror.sync()
        local = self.repo.find_assignment(None, "Update Resume")
        self.assertIsNotNone(local)
        self.assertIsNone(local["course"])

    def test_a_row_awaiting_its_first_push_is_linked_not_duplicated(self):
        """The trap: the sheet row and the hand-made task are the same work."""
        assignment = self.repo.create_assignment("HW9", course="CS 4262")
        self.hand_made("HW9")
        self.mirror.sync()
        rows = [r for r in self.repo.all_assignments() if r["name"] == "HW9"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["id"]), assignment)
        live = [t for t in self.tasks.tasks.values()
                if t["title"] == "HW9" and not t.get("deleted")]
        self.assertEqual(len(live), 1)

    def test_a_row_deleted_from_the_sheet_is_not_resurrected(self):
        """The other trap: dropping a row must not re-adopt its lingering task."""
        # A second row, so removing the adopted one is a deletion rather than
        # the empty sheet the read guard refuses to act on.
        self.add_sheet_row("HW1", sheet_id="abc")
        self.hand_made("Update Resume")
        self.mirror.sync()
        self.sheets.grid = [
            row for row in self.sheets.grid if "Update Resume" not in row
        ]
        report = self.mirror.sync()
        self.assertEqual(report.dropped, ["CS 4262 · Update Resume"])

        again = self.mirror.sync(full=True)
        self.assertEqual(again.adopted_from_tasks, [])
        rows = [r for r in self.repo.all_assignments() if r["name"] == "Update Resume"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "dropped")

    def test_it_can_be_turned_off(self):
        self.hand_made("Update Resume")
        with unittest.mock.patch.object(config, "ADOPT_NEW_TASKS", False):
            report = self.mirror.sync()
        self.assertEqual(report.adopted_from_tasks, [])
        with self.assertRaises(AssertionError):
            self.sheets.row_for("Update Resume")

    def test_an_excluded_list_is_never_adopted_from(self):
        self.tasks.tasklists["lx"] = "Shared"
        self.hand_made("Someone else's task", tasklist="lx")
        with unittest.mock.patch.object(config, "TASKLIST_EXCLUDE", ("Shared",)):
            report = self.mirror.sync()
        self.assertEqual(report.adopted_from_tasks, [])


class TestSeeding(MirrorCase):
    def test_existing_tasks_become_sheet_rows(self):
        self.tasks.insert("l1", "Quiz 2", due="2026-09-15T00:00:00.000Z")
        done = self.tasks.insert("l1", "Quiz 1")
        self.tasks.patch("l1", done["id"], {"status": "completed"})

        created = self.mirror.seed_from_tasks()
        self.assertEqual(created, 2)
        self.mirror.sync()

        self.assertEqual(self.sheets.row_for("Quiz 2")["Type"], "quiz")
        self.assertEqual(self.sheets.row_for("Quiz 1")["% done"], "100")
        self.assertEqual(len(self.repo.candidates()), 1)  # the finished one drops out

    def test_seeding_twice_does_not_duplicate(self):
        self.tasks.insert("l1", "Quiz 2")
        self.mirror.seed_from_tasks()
        self.assertEqual(self.mirror.seed_from_tasks(), 0)

    def test_kind_guessing(self):
        self.assertEqual(guess_kind("Midterm Exam 1"), "exam")
        self.assertEqual(guess_kind("Sprint 2 Demo"), "project")
        self.assertEqual(guess_kind("HW1"), "homework")
        self.assertIsNone(guess_kind("RFTH"))


class TestCellFormats(unittest.TestCase):
    def test_durations_round_trip(self):
        for text, minutes in [("3h", 180), ("90m", 90), ("1h30", 90), ("2.5h", 150)]:
            self.assertEqual(gsheets.parse_duration(text), minutes)
        self.assertEqual(gsheets.format_duration(195), "3h15")
        self.assertEqual(gsheets.format_duration(None), "")

    def test_a_bare_number_means_hours(self):
        self.assertEqual(gsheets.parse_duration("3"), 180)

    def test_unreadable_values_do_not_raise(self):
        with self.assertLogs("nudge.connectors.gsheets", level="WARNING"):
            self.assertIsNone(gsheets.parse_duration("ages"))
        with self.assertLogs("nudge.connectors.gsheets", level="WARNING"):
            self.assertEqual(gsheets.parse_percent("nearly"), 0.0)

    def test_dates_accept_several_shapes(self):
        for text in ("2026-09-17", "9/17/2026", "Sep 17"):
            parsed = gsheets.parse_date(text)
            self.assertEqual(gsheets.format_date(parsed), "2026-09-17")

    def test_a_due_date_is_the_end_of_the_local_day(self):
        parsed = gsheets.parse_date("2026-09-17")
        self.assertEqual(tu.to_local(parsed).hour, 23)


if __name__ == "__main__":
    unittest.main()


class TestRecurrence(MirrorCase):
    def test_rules_parse_in_several_shapes(self):
        from nudge import recurrence

        self.assertEqual(recurrence.parse_rule("weekly:fri"), {4})
        self.assertEqual(recurrence.parse_rule("sun,tue"), {6, 1})
        self.assertEqual(recurrence.parse_rule("every Monday"), {0})
        self.assertIsNone(recurrence.parse_rule(""))

    def test_a_finished_occurrence_rolls_to_the_next_one(self):
        self.add_sheet_row("RFTH", course="CS 4726", repeat="weekly:fri",
                           expected="1h", sheet_id="r1")
        self.mirror.sync()
        assignment = int(self.repo.assignment_by_sheet_id("r1")["id"])
        first_due = self.repo.get_assignment(assignment)["due_at"]

        self.repo.set_percent(assignment, 100)
        report = self.mirror.sync()

        row = self.repo.get_assignment(assignment)
        self.assertEqual(report.rolled, ["CS 4726 · RFTH"])
        self.assertEqual(row["percent_done"], 0)
        self.assertEqual(row["status"], "active")
        self.assertGreater(row["due_at"], first_due)
        self.assertEqual(tu.to_local(tu.parse(row["due_at"])).weekday(), 4)

    def test_a_missed_occurrence_rolls_forward_too(self):
        self.add_sheet_row("RFTH", course="CS 4726", repeat="weekly:fri",
                           due="2026-01-02", expected="1h", sheet_id="r1")
        self.mirror.sync()
        row = self.repo.assignment_by_sheet_id("r1")
        self.assertGreater(tu.parse(row["due_at"]), tu.now_utc())

    def test_an_unfinished_future_occurrence_is_left_alone(self):
        self.add_sheet_row("RFTH", course="CS 4726", repeat="weekly:fri",
                           expected="1h", sheet_id="r1")
        self.mirror.sync()
        before = self.repo.assignment_by_sheet_id("r1")["due_at"]
        report = self.mirror.sync()
        self.assertEqual(report.rolled, [])
        self.assertEqual(self.repo.assignment_by_sheet_id("r1")["due_at"], before)

    def test_two_days_a_week(self):
        self.add_sheet_row("MGT readings", course="MGT074", repeat="sun,tue",
                           expected="10m", sheet_id="r2")
        self.mirror.sync()
        row = self.repo.assignment_by_sheet_id("r2")
        self.assertIn(tu.to_local(tu.parse(row["due_at"])).weekday(), (6, 1))

    def test_the_rule_round_trips_through_the_sheet(self):
        self.mirror.sync()
        self.repo.create_assignment(
            "RFTH", course="CS 4726", repeat_rule="weekly:fri", expected_min=60
        )
        self.mirror.sync()
        self.assertEqual(self.sheets.row_for("RFTH")["Repeats"], "weekly:fri")


class TestSeedLinking(MirrorCase):
    def test_seeding_links_a_row_that_already_exists_locally(self):
        # Rows added before the first sync must not spawn duplicate tasks.
        self.repo.create_assignment("HW1", course="CS 4262", expected_min=480)
        self.tasks.insert("l1", "HW1", due="2026-09-17T00:00:00.000Z")

        self.assertEqual(self.mirror.seed_from_tasks(), 0)
        self.mirror.sync()
        matching = [t for t in self.tasks.tasks.values() if t["title"] == "HW1"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(len(self.repo.candidates()), 1)


class TestSchemaUpgrade(unittest.TestCase):
    """A sheet made before a column existed must gain it without shuffling."""

    def setUp(self):
        self.repo = Repo(db.connect(":memory:"))
        self.addCleanup(self.repo.conn.close)
        # An older sheet: no Repeats column, and Class before Assignment.
        self.sheets = FakeSheets([
            ["Class", "Assignment", "Due date", "Type", "Expected length",
             "% done", "Relevant files", "ID"],
            ["CS 4262", "HW1", "2026-09-17", "homework", "8h", "0", "", "abc"],
        ])
        self.tasks = FakeTasks()
        self.mirror = Mirror(self.repo, self.sheets, self.tasks)

    def test_the_missing_columns_are_appended_to_the_header(self):
        self.mirror.sync()
        self.assertEqual(self.sheets.grid[0][-2:], ["Repeats", "Not before"])
        self.assertEqual(self.sheets.grid[0][:2], ["Class", "Assignment"])

    def test_existing_rows_still_parse_after_the_upgrade(self):
        self.mirror.sync()
        candidate = self.repo.candidates()[0]
        self.assertEqual(candidate.name, "HW1")
        self.assertEqual(candidate.expected_min, 480)

    def test_a_new_row_lands_in_the_sheet_own_column_order(self):
        self.mirror.sync()
        self.repo.create_assignment(
            "Weekly quiz", course="CS 4641", expected_min=30, repeat_rule="weekly:mon"
        )
        self.mirror.sync()
        header = self.sheets.grid[0]
        row = next(r for r in self.sheets.grid if len(r) > 1 and r[1] == "Weekly quiz")
        cells = dict(zip(header, row))
        self.assertEqual(cells["Class"], "CS 4641")
        self.assertEqual(cells["Assignment"], "Weekly quiz")
        self.assertEqual(cells["Expected length"], "30m")
        self.assertEqual(cells["Repeats"], "weekly:mon")


class TestEmptyReadGuard(MirrorCase):
    """An empty sheet read must never be read as "everything was deleted"."""

    def test_an_empty_read_aborts_instead_of_dropping_everything(self):
        self.add_sheet_row("HW1", sheet_id="abc")
        self.add_sheet_row("Quiz 2", sheet_id="def")
        self.mirror.sync()
        self.assertEqual(len(self.repo.candidates()), 2)

        self.sheets.grid = []  # transient API hiccup
        with self.assertLogs("nudge.mirror", level="ERROR"):
            report = self.mirror.sync()

        self.assertIsNotNone(report.aborted)
        self.assertEqual(report.dropped, [])
        self.assertEqual(len(self.repo.candidates()), 2)

    def test_a_genuinely_empty_first_run_is_fine(self):
        report = self.mirror.sync()
        self.assertIsNone(report.aborted)

    def test_an_unreadable_header_does_not_get_stamped_over(self):
        self.sheets.grid = [["what", "is", "this"], ["some", "stray", "data"]]
        with self.assertLogs("nudge.mirror", level="ERROR"):
            self.mirror.sync()
        self.assertEqual(self.sheets.grid[0], ["what", "is", "this"])

    def test_deleting_one_row_still_drops_it(self):
        self.add_sheet_row("HW1", sheet_id="abc")
        self.add_sheet_row("Quiz 2", sheet_id="def")
        self.mirror.sync()
        del self.sheets.grid[1]
        report = self.mirror.sync()
        self.assertEqual(len(report.dropped), 1)


class TestSheetIds(unittest.TestCase):
    def test_ids_never_look_numeric(self):
        # Sheets reads "202e65" as scientific notation and eats leading zeros.
        for _ in range(200):
            generated = gsheets.new_sheet_id()
            self.assertTrue(generated[0].isalpha(), generated)
            with self.assertRaises(ValueError):
                float(generated)


class TestNotBefore(MirrorCase):
    """Work that cannot be started yet must not be offered, however urgent."""

    def test_rules_resolve_against_the_due_date(self):
        from nudge import recurrence

        friday = gsheets.parse_date("2026-09-11")     # Fri 23:59 local
        monday = gsheets.parse_date("2026-09-14")     # Mon 23:59 local

        opens = recurrence.resolve_not_before(
            recurrence.parse_not_before("fri 10am"), friday
        )
        self.assertEqual(tu.to_local(opens).strftime("%a %H:%M"), "Fri 10:00")

        # A quiz released Friday 5pm but due Monday opens the Friday *before*.
        opens = recurrence.resolve_not_before(
            recurrence.parse_not_before("fri 5pm"), monday
        )
        self.assertEqual(tu.to_local(opens).strftime("%a %H:%M"), "Fri 17:00")

    def test_an_unreadable_rule_is_ignored_rather_than_guessed(self):
        from nudge import recurrence

        self.assertIsNone(recurrence.parse_not_before("whenever"))
        self.assertIsNone(recurrence.parse_not_before(""))

    def test_it_is_not_offered_before_it_opens(self):
        self.add_sheet_row("RFTH", course="CS 4726", due="2026-09-11",
                           expected="30m", not_before="fri 10am", sheet_id="r1")
        self.mirror.sync()
        candidate = self.repo.candidates()[0]
        self.assertIsNotNone(candidate.available_from)

        from nudge.engine import scoring

        before = tu.parse("2026-09-09T16:00:00+00:00")   # Wednesday noon local
        after = tu.parse("2026-09-11T15:00:00+00:00")    # Friday 11am local
        args = dict(usable_gap_min=60, continuity_assignment_id=None,
                    last_decline_lookup=lambda _i: None)
        self.assertEqual(scoring.rank([candidate], now=before, **args), [])
        self.assertEqual(len(scoring.rank([candidate], now=after, **args)), 1)

    def test_the_capacity_window_starts_when_it_opens(self):
        from nudge.engine import capacity

        now = tu.parse("2026-09-07T16:00:00+00:00")     # Monday
        due = gsheets.parse_date("2026-09-11")
        opens = tu.parse("2026-09-11T14:00:00+00:00")   # Friday 10am local
        whole_week = capacity.available_minutes(now, due)
        friday_only = capacity.available_minutes(now, due, opens_at=opens)
        self.assertLess(friday_only, whole_week)

    def test_rolling_forward_moves_the_opening_too(self):
        self.add_sheet_row("RFTH", course="CS 4726", repeat="weekly:fri",
                           expected="30m", not_before="fri 10am", sheet_id="r1")
        self.mirror.sync()
        first = self.repo.assignment_by_sheet_id("r1")

        self.repo.set_percent(int(first["id"]), 100)
        self.mirror.sync()
        second = self.repo.assignment_by_sheet_id("r1")

        self.assertGreater(second["available_from"], first["available_from"])
        opens = tu.to_local(tu.parse(second["available_from"]))
        due = tu.to_local(tu.parse(second["due_at"]))
        self.assertEqual(opens.strftime("%a %H:%M"), "Fri 10:00")
        self.assertEqual(opens.date(), due.date())

    def test_the_rule_round_trips_through_the_sheet(self):
        self.mirror.sync()
        self.repo.create_assignment(
            "RFTH", course="CS 4726", expected_min=30,
            repeat_rule="weekly:fri", not_before="fri 10am",
        )
        self.mirror.sync()
        self.assertEqual(self.sheets.row_for("RFTH")["Not before"], "fri 10am")


class TestClassRename(MirrorCase):
    def test_renaming_a_class_does_not_split_its_task_list(self):
        self.add_sheet_row("HW1", course="CS 4262", sheet_id="a1")
        self.mirror.sync()
        original_list = self.repo.assignment_by_sheet_id("a1")["gtasklist_id"]

        # rename the class in the sheet, then add a second assignment to it
        self.sheets.grid[1][gsheets.CANONICAL.index("course")] = "CS 4262 Networks"
        self.add_sheet_row("HW2", course="CS 4262 Networks", sheet_id="a2")
        self.mirror.sync()

        self.assertEqual(
            self.repo.assignment_by_sheet_id("a2")["gtasklist_id"], original_list
        )
        self.assertNotIn("CS 4262 Networks", self.tasks.tasklists.values())


class TestSheetEditsReachTasks(MirrorCase):
    """The whole point of the cron: Tasks has to represent the sheet."""

    def setUp(self):
        super().setUp()
        self.add_sheet_row("HW1", due="2026-09-17", expected="3h", sheet_id="abc")
        self.mirror.sync()

    def edit(self, column: str, value: str) -> None:
        self.sheets.grid[1][gsheets.CANONICAL.index(column)] = value

    def test_a_due_date_edited_in_the_sheet_updates_the_task(self):
        self.edit("due", "2026-09-30")
        self.mirror.sync()
        self.assertEqual(self.tasks.task_named("HW1")["due"][:10], "2026-09-30")

    def test_a_rename_in_the_sheet_updates_the_task(self):
        self.edit("name", "Homework 1")
        self.mirror.sync()
        self.assertEqual(self.tasks.task_named("Homework 1")["title"], "Homework 1")

    def test_a_percentage_typed_into_the_sheet_reaches_the_task(self):
        self.edit("percent", "100")
        self.mirror.sync()
        self.assertEqual(self.tasks.task_named("HW1")["status"], "completed")

    def test_an_untouched_row_is_not_pushed_every_sync(self):
        self.mirror.sync()
        before = dict(self.tasks.task_named("HW1"))
        report = self.mirror.sync()
        self.assertEqual(report.pushed_to_tasks, 0)
        self.assertEqual(self.tasks.task_named("HW1")["updated"], before["updated"])

    def test_editing_a_column_tasks_never_shows_does_not_churn(self):
        self.edit("kind", "written")
        report = self.mirror.sync()
        self.assertEqual(report.pushed_to_tasks, 0)


class TestReopenIsNotUndone(MirrorCase):
    def test_reopening_a_task_survives_the_sync_that_notices_it(self):
        self.add_sheet_row("Quiz", expected="1h", sheet_id="q1")
        self.mirror.sync()
        assignment = int(self.repo.assignment_by_sheet_id("q1")["id"])
        self.repo.set_percent(assignment, 100)
        self.mirror.sync()

        task = self.tasks.task_named("Quiz")
        self.tasks.patch("l1", task["id"], {"status": "needsAction"})
        report = self.mirror.sync()

        self.assertEqual(report.reopened_elsewhere, ["CS 4262 · Quiz"])
        self.assertEqual(self.tasks.task_named("Quiz")["status"], "needsAction")
        self.assertEqual(self.tasks.task_named("Quiz")["status"], "needsAction")

    def test_it_stays_reopened_across_further_syncs(self):
        self.add_sheet_row("Quiz", expected="1h", sheet_id="q1")
        self.mirror.sync()
        assignment = int(self.repo.assignment_by_sheet_id("q1")["id"])
        self.repo.set_percent(assignment, 100)
        self.mirror.sync()
        self.tasks.patch("l1", self.tasks.task_named("Quiz")["id"],
                         {"status": "needsAction"})
        self.mirror.sync()
        self.mirror.sync()
        self.assertEqual(self.tasks.task_named("Quiz")["status"], "needsAction")

    def test_answering_with_a_percentage_settles_it_everywhere(self):
        self.add_sheet_row("Quiz", expected="1h", sheet_id="q1")
        self.mirror.sync()
        assignment = int(self.repo.assignment_by_sheet_id("q1")["id"])
        self.repo.set_percent(assignment, 100)
        self.mirror.sync()
        self.tasks.patch("l1", self.tasks.task_named("Quiz")["id"],
                         {"status": "needsAction"})
        self.mirror.sync()

        self.repo.set_percent(assignment, 50)   # "50%" in Discord
        self.mirror.sync()
        self.assertEqual(self.sheets.row_for("Quiz")["% done"], "50")
        self.assertEqual(self.tasks.task_named("Quiz")["status"], "needsAction")
        self.assertEqual(self.tasks.task_named("Quiz")["notes"], "50% done")


class TestRowIdentityMishaps(MirrorCase):
    """People edit spreadsheets by hand. Identity has to survive that."""

    def test_clearing_an_id_cell_reattaches_instead_of_duplicating(self):
        self.add_sheet_row("HW1", expected="2h", sheet_id="abc")
        self.mirror.sync()

        self.sheets.grid[1][gsheets.CANONICAL.index("sheet_id")] = ""
        report = self.mirror.sync()

        self.assertEqual(report.dropped, [])
        self.assertEqual(len(self.repo.all_assignments()), 1)
        live = [t for t in self.tasks.tasks.values() if not t.get("deleted")]
        self.assertEqual(len(live), 1)
        self.assertEqual(self.sheets.row_for("HW1")["ID"], "abc")

    def test_a_copied_row_becomes_its_own_assignment(self):
        self.add_sheet_row("HW1", expected="2h", sheet_id="abc")
        self.mirror.sync()
        self.add_sheet_row("HW1 copy", expected="2h", sheet_id="abc")  # paste
        self.mirror.sync()

        names = sorted(a["name"] for a in self.repo.all_assignments())
        self.assertEqual(names, ["HW1", "HW1 copy"])
        original = self.repo.assignment_by_sheet_id("abc")
        self.assertEqual(original["name"], "HW1")
        self.assertNotEqual(self.sheets.row_for("HW1 copy")["ID"], "abc")

    def test_a_genuinely_new_row_is_still_new(self):
        self.add_sheet_row("HW1", expected="2h", sheet_id="abc")
        self.mirror.sync()
        self.add_sheet_row("HW2", expected="2h")   # no id, different name
        report = self.mirror.sync()
        self.assertEqual(report.new_from_sheet, ["CS 4262 · HW2"])
        self.assertEqual(len(self.repo.all_assignments()), 2)

    def test_adoption_does_not_steal_a_row_already_matched(self):
        # Two rows with the same name: the second must not adopt the first.
        self.add_sheet_row("Reading", expected="1h", sheet_id="r1")
        self.mirror.sync()
        self.add_sheet_row("Reading", expected="1h")   # same name, no id
        self.mirror.sync()
        ids = {a["sheet_id"] for a in self.repo.all_assignments()}
        self.assertEqual(len(ids), 2)
