"""Sheet <-> SQLite <-> Google Tasks.

The sheet is the source of truth for what exists and how far along it is.
SQLite mirrors it and adds everything scheduling-related. Tasks is a second
mirror you can also tick things off in, and those completions flow back — as
does a task you create there yourself, which becomes a new row in the sheet.

Order matters: pull the sheet first (it is truth), then Tasks (completions are
new information), then push both. A row whose local copy has unpushed changes
is never overwritten by what the sheet still shows — the push resolves it in
the same pass.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import config
from . import recurrence
from . import timeutil as tu
from .connectors import gsheets, gtasks
from .connectors.gsheets import SheetsClient, column_letter
from .connectors.gtasks import TasksClient
from .store.repo import Repo

log = logging.getLogger(__name__)


@dataclass(slots=True)
class MirrorReport:
    sheet_rows: int = 0
    tasks_pulled: int = 0
    pushed_to_sheet: int = 0
    pushed_to_tasks: int = 0
    new_from_sheet: list[str] = field(default_factory=list)
    adopted_from_tasks: list[str] = field(default_factory=list)
    completed_elsewhere: list[str] = field(default_factory=list)
    reopened_elsewhere: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    rolled: list[str] = field(default_factory=list)
    aborted: str | None = None

    def __str__(self) -> str:
        if self.aborted:
            return f"aborted: {self.aborted}"
        return (
            f"{self.sheet_rows} sheet rows, {self.tasks_pulled} task updates, "
            f"{self.pushed_to_sheet}->sheet, {self.pushed_to_tasks}->tasks, "
            f"{len(self.new_from_sheet)} new, "
            f"{len(self.adopted_from_tasks)} adopted from tasks, "
            f"{len(self.completed_elsewhere)} completed elsewhere"
        )


def label(row) -> str:
    return f"{row['course']} · {row['name']}" if row["course"] else row["name"]


def _awaiting_percentage(local) -> bool:
    """Reopened in Tasks, with no replacement percentage supplied yet.

    Needs no extra state: "active but 100% done" is only reachable that way.
    """
    return local["status"] == "active" and float(local["percent_done"] or 0) >= 100


def _task_fields_differ(local, values: dict, status: str) -> bool:
    """Has anything Google Tasks shows changed? Title, due date, progress."""
    if (local["name"] or "") != (values["name"] or ""):
        return True
    if (local["due_at"] or None) != tu.iso(values["due_at"]):
        return True
    if round(float(local["percent_done"] or 0)) != round(values["percent_done"]):
        return True
    return local["status"] != status


class Mirror:
    def __init__(
        self,
        repo: Repo,
        sheets: SheetsClient | None = None,
        tasks: TasksClient | None = None,
    ) -> None:
        self.repo = repo
        self.sheets = sheets or SheetsClient()
        self.tasks = tasks or TasksClient()
        self._list_ids: dict[str, str] = {}

    # --- the spreadsheet ------------------------------------------------
    def spreadsheet_id(self, create: bool = True) -> str | None:
        existing = self.repo.kv_get(gsheets.SPREADSHEET_ID)
        if existing or not create:
            return existing
        spreadsheet_id = self.sheets.create()
        self.repo.kv_set(gsheets.SPREADSHEET_ID, spreadsheet_id)
        log.info("created spreadsheet %s", self.sheets.url(spreadsheet_id))
        return spreadsheet_id

    @property
    def url(self) -> str | None:
        spreadsheet_id = self.spreadsheet_id(create=False)
        return self.sheets.url(spreadsheet_id) if spreadsheet_id else None

    # --- entry point ----------------------------------------------------
    def sync(self, *, full: bool = False) -> MirrorReport:
        report = MirrorReport()
        spreadsheet_id = self.spreadsheet_id()
        index, rows = self.sheets.read(spreadsheet_id)

        # An empty read is far more likely to be a transient API problem than a
        # user deleting every row at once. Treating it as deletion drops the
        # whole mirror, so refuse to act on it at all.
        if not rows and self.repo.previously_synced_count():
            report.aborted = "the sheet came back empty; refusing to treat that as deletion"
            log.error("%s", report.aborted)
            return report

        index = self._ensure_columns(spreadsheet_id, index)
        report.sheet_rows = len(rows)

        self._pull_sheet(spreadsheet_id, index, rows, report)
        # After the pull, so a row that has just arrived (or just come back
        # with a stale date) gets its next occurrence in this same pass.
        self.roll_recurring(tu.now_utc(), report)
        self._pull_tasks(report, full=full)
        self._push_sheet(spreadsheet_id, index, rows, report)
        self._push_tasks(report)

        log.info("mirror: %s", report)
        return report

    def roll_recurring(self, now: datetime, report: MirrorReport) -> None:
        """Move a repeating assignment on to its next occurrence.

        Once it is finished — or once its due date has passed, because a weekly
        quiz you missed is simply gone — the row rolls forward and starts again
        at 0%. The offers log keeps the history.
        """
        for row in self.repo.recurring():
            days = recurrence.parse_rule(row["repeat_rule"])
            if not days:
                continue
            due_at = tu.parse(row["due_at"])
            finished = float(row["percent_done"] or 0) >= 100
            if due_at is not None and due_at > now and not finished:
                continue
            if finished and due_at is not None and due_at > now:
                # Finished this week's early — the next one is after the date
                # just completed, not after today.
                next_at = recurrence.next_due(days, due_at)
            else:
                next_at = recurrence.due_now(days, now)
            self.repo.update_assignment(
                int(row["id"]),
                due_at=next_at,
                available_from=recurrence.resolve_not_before(
                    recurrence.parse_not_before(row["not_before"]), next_at
                ),
                percent_done=0.0,
                status="active",
            )
            self.repo.clear_snooze(int(row["id"]))
            self.repo.reset_declines(int(row["id"]))
            report.rolled.append(label(row))

    def _ensure_columns(self, spreadsheet_id, index: dict[str, int]) -> dict[str, int]:
        """Add any column the sheet is missing, e.g. after an upgrade.

        Existing columns keep their positions — people reorder these.
        """
        missing = [name for name in gsheets.CANONICAL if name not in index]
        if not missing:
            return index
        if len(missing) == len(gsheets.CANONICAL):
            # No column matched at all. Stamping a fresh header over a sheet
            # whose header we simply failed to read would shift every row.
            log.error("no recognisable header row; leaving the sheet alone")
            return index
        index = dict(index)
        position = (max(index.values()) + 1) if index else 0
        writes = []
        for name in missing:
            index[name] = position
            writes.append((f"{column_letter(position)}1", gsheets.header_for(name)))
            position += 1
        self.sheets.update_cells(spreadsheet_id, writes)
        log.info("added sheet columns: %s", ", ".join(w[1] for w in writes))
        return index

    # --- sheet -> local -------------------------------------------------
    def _pull_sheet(self, spreadsheet_id, index, rows, report: MirrorReport) -> None:
        if "name" not in index:  # note: column 0 is falsy, so test membership
            log.warning("the sheet has no recognisable 'Assignment' column")
            return

        seen: set[str] = set()
        id_writes: list[tuple[str, object]] = []
        id_column = index.get("sheet_id")

        for row in rows:
            name = row.get("name")
            if not name:
                continue
            sheet_id = row.get("sheet_id")
            if sheet_id and sheet_id in seen:
                # A copied row carries the original's id. Treated naively the
                # copy would take over the original's identity, renaming it.
                log.info("row %d duplicates id %s; giving it a new one",
                         row.row_number, sheet_id)
                sheet_id = ""
            if not sheet_id:
                # An id cell someone cleared should find its way home rather
                # than spawning a second assignment and a second task.
                adopted = self.repo.find_assignment(row.get("course") or None, name)
                if adopted and adopted["sheet_id"] and adopted["sheet_id"] not in seen:
                    sheet_id = adopted["sheet_id"]
                else:
                    sheet_id = gsheets.new_sheet_id()
                if id_column is not None:
                    id_writes.append(
                        (f"{column_letter(id_column)}{row.row_number}", sheet_id)
                    )
            seen.add(sheet_id)

            values = {
                "name": name,
                "course": row.get("course") or None,
                "kind": row.get("kind") or None,
                "due_at": gsheets.parse_date(row.get("due")),
                "expected_min": gsheets.parse_duration(row.get("expected")),
                "percent_done": gsheets.parse_percent(row.get("percent")),
                "files": row.get("files") or None,
                "repeat_rule": row.get("repeat") or None,
                "not_before": row.get("not_before") or None,
            }
            values["available_from"] = recurrence.resolve_not_before(
                recurrence.parse_not_before(values["not_before"]), values["due_at"]
            )

            local = self.repo.assignment_by_sheet_id(sheet_id)
            if local is None:
                assignment_id = self.repo.create_assignment(
                    values["name"],
                    sheet_id=sheet_id,
                    course=values["course"],
                    kind=values["kind"],
                    due_at=values["due_at"],
                    expected_min=values["expected_min"],
                    percent_done=values["percent_done"],
                    files=values["files"],
                    repeat_rule=values["repeat_rule"],
                    not_before=values["not_before"],
                    available_from=values["available_from"],
                )
                if values["percent_done"] >= 100:
                    self.repo.set_status(assignment_id, "done", dirty=False)
                self.repo.mark_sheet_synced(assignment_id)
                report.new_from_sheet.append(
                    f"{values['course']} · {name}" if values["course"] else name
                )
                continue

            if local["sheet_synced_at"] is None:
                continue  # unpushed local change wins; _push_sheet resolves it

            status = "done" if values["percent_done"] >= 100 else "active"
            if status == "done" and _awaiting_percentage(local):
                # Reopened in the Tasks app and not answered yet. The sheet
                # still reads 100%, so without this the next sync would close
                # the task again underneath you.
                status = "active"
            # The sheet is already correct, so this is not a sheet push — but
            # anything Tasks displays has to be forwarded, or an edit made in
            # the sheet never reaches the task.
            task_changed = _task_fields_differ(local, values, status)
            self.repo.update_assignment(
                int(local["id"]), **values, status=status, dirty=False
            )
            self.repo.mark_sheet_synced(int(local["id"]))
            if task_changed:
                self.repo.mark_task_dirty(int(local["id"]))

        if id_writes:
            self.sheets.update_cells(spreadsheet_id, id_writes)

        # A row deleted from the sheet is gone: the sheet is the source of truth.
        # Only rows that actually reached the sheet can have been deleted from
        # it — a locally created one is simply waiting for its first push.
        for local in self.repo.all_assignments():
            if (
                local["sheet_id"]
                and local["sheet_synced_at"]
                and local["sheet_id"] not in seen
            ):
                if local["status"] != "dropped":
                    self.repo.set_status(int(local["id"]), "dropped", dirty=False)
                    self.repo.mark_sheet_synced(int(local["id"]))
                    report.dropped.append(label(local))

    # --- tasks -> local -------------------------------------------------
    def _tasklists(self) -> list[tuple[str, str]]:
        items = self.tasks.list_tasklists()
        if config.TASKLISTS is not None:
            items = [i for i in items if i["title"] in config.TASKLISTS]
        result = [
            (i["id"], i["title"])
            for i in items
            if i["title"] not in config.TASKLIST_EXCLUDE
        ]
        self._list_ids = {title: list_id for list_id, title in result}
        return result

    def _pull_tasks(self, report: MirrorReport, *, full: bool = False) -> None:
        started = tu.now_utc()
        for tasklist_id, title in self._tasklists():
            key = f"{gtasks.SYNC_CURSOR}:{tasklist_id}"
            cursor = None if full else self.repo.kv_get(key)
            for task in self.tasks.list_tasks(tasklist_id, updated_min=cursor):
                report.tasks_pulled += 1
                self._pull_task(task, report, tasklist_id, title)
            self.repo.kv_set(
                key,
                gtasks.rfc3339(
                    started - timedelta(minutes=config.SYNC_LOOKBACK_MINUTES)
                ),
            )

    def _pull_task(
        self,
        task: dict,
        report: MirrorReport,
        tasklist_id: str | None = None,
        course: str | None = None,
    ) -> None:
        local = self.repo.assignment_by_gtask(task["id"])
        if local is None:
            # A task typed straight into the Tasks app. Adopting it here is what
            # makes "add it in Tasks, see it in the sheet" work: the row is
            # created now and _push_sheet appends it later in this same pass.
            if not config.ADOPT_NEW_TASKS or tasklist_id is None:
                return
            adopted = self._adopt_task(task, tasklist_id, course)
            if adopted is not None:
                report.adopted_from_tasks.append(
                    label(self.repo.get_assignment(adopted))
                )
            return
        assignment_id = int(local["id"])

        if task.get("deleted"):
            self.repo.clear_gtask(assignment_id)  # a later push re-creates it
            return
        if local["synced_at"] is None:
            return  # unpushed local change wins

        completed = task.get("status") == "completed"
        percent = float(local["percent_done"] or 0)
        if completed and percent < 100:
            self.repo.set_percent(assignment_id, 100)
            self.repo.mark_task_synced(assignment_id)
            report.completed_elsewhere.append(label(local))
        elif not completed and percent >= 100:
            # Reopened in the Tasks app. The percent is the sheet's to set, so
            # nudge asks rather than inventing a number — and crucially does not
            # mark this dirty, or the push below would re-complete the task the
            # moment you reopened it.
            self.repo.set_status(assignment_id, "active", dirty=False)
            report.reopened_elsewhere.append(label(local))

    # --- local -> sheet -------------------------------------------------
    def _push_sheet(self, spreadsheet_id, index, rows, report: MirrorReport) -> None:
        by_sheet_id = {row.get("sheet_id"): row for row in rows if row.get("sheet_id")}
        updates: list[tuple[str, object]] = []
        appends: list[list] = []

        for local in self.repo.needing_sheet_push():
            cells = self._cells_for(local)
            row = by_sheet_id.get(local["sheet_id"])
            if row is None:
                sheet_id = local["sheet_id"] or gsheets.new_sheet_id()
                if not local["sheet_id"]:
                    self.repo.update_assignment(
                        int(local["id"]), sheet_id=sheet_id, dirty=False
                    )
                cells["sheet_id"] = sheet_id
                # Positional by the sheet's own header order, not the canonical
                # one — the columns may have been reordered.
                width = max(index.values()) + 1 if index else len(gsheets.CANONICAL)
                blank = [""] * width
                for name, value in cells.items():
                    if name in index:
                        blank[index[name]] = value
                appends.append(blank)
            else:
                for name, value in cells.items():
                    position = index.get(name)
                    if position is None:
                        continue
                    if row.get(name) == str(value):
                        continue
                    updates.append(
                        (f"{column_letter(position)}{row.row_number}", value)
                    )
            self.repo.mark_sheet_synced(int(local["id"]))
            report.pushed_to_sheet += 1

        self.sheets.update_cells(spreadsheet_id, updates)
        self.sheets.append(spreadsheet_id, appends)

    def _cells_for(self, local) -> dict[str, object]:
        return {
            "name": local["name"],
            "course": local["course"] or "",
            "due": gsheets.format_date(tu.parse(local["due_at"])),
            "kind": local["kind"] or "",
            "expected": gsheets.format_duration(local["expected_min"]),
            "percent": gsheets.format_percent(local["percent_done"]),
            "repeat": local["repeat_rule"] or "",
            "not_before": local["not_before"] or "",
            "files": local["files"] or "",
        }

    # --- local -> tasks -------------------------------------------------
    def _tasklist_for(self, local) -> str:
        if local["gtasklist_id"]:
            return local["gtasklist_id"]
        course = local["course"] or config.DEFAULT_TASKLIST
        if course in self._list_ids:
            return self._list_ids[course]
        # Renaming a class in the sheet must not split its tasks across two
        # lists. If anything else in this class already lives somewhere, so
        # does this.
        sibling = self.repo.tasklist_for_course(local["course"])
        if sibling:
            self._list_ids[course] = sibling
            return sibling
        self._list_ids[course] = self.tasks.ensure_tasklist(course)
        return self._list_ids[course]

    def _push_tasks(self, report: MirrorReport) -> None:
        if not self._list_ids:
            self._tasklists()
        for local in self.repo.needing_task_push():
            assignment_id = int(local["id"])
            tasklist_id = self._tasklist_for(local)
            due = gtasks.to_due(tu.parse(local["due_at"]))
            percent = float(local["percent_done"] or 0)
            notes = f"{int(round(percent))}% done" if 0 < percent < 100 else None

            if not local["gtask_id"]:
                task = self.tasks.insert(
                    tasklist_id, local["name"], notes=notes, due=due
                )
                self.repo.set_gtask(assignment_id, task["id"], tasklist_id)
                if percent >= 100:
                    self.tasks.patch(
                        tasklist_id, task["id"], {"status": "completed"}
                    )
            else:
                # notes is always written, so a stale "40% done" cannot linger
                # on a task that has since been finished.
                self.tasks.patch(
                    tasklist_id,
                    local["gtask_id"],
                    {
                        "title": local["name"],
                        "due": due,
                        "notes": notes or "",
                        "status": (
                            "completed"
                            if percent >= 100 and not _awaiting_percentage(local)
                            else "needsAction"
                        ),
                    },
                )
            self.repo.mark_task_synced(assignment_id)
            report.pushed_to_tasks += 1

    # --- adopting a task nudge did not create ---------------------------
    def _adopt_task(
        self, task: dict, tasklist_id: str, course: str | None
    ) -> int | None:
        """Turn a task nudge has never seen into an assignment linked to it.

        Shared by the first-run seed and the ordinary reconcile, so the two
        import paths cannot drift. Returns the new assignment id, or None when
        the task should not become a row of its own.

        The row is left dirty for the sheet (it has to be appended) but marked
        synced to Tasks — the task already exists, and pushing it back would
        create a second copy of the work.
        """
        if task.get("parent") or task.get("deleted"):
            return None  # a subtask is a step, not an assignment
        title = (task.get("title") or "").strip()
        if not title:
            return None
        if course == config.DEFAULT_TASKLIST:
            course = None  # the catch-all list means "no class"

        existing = self.repo.find_assignment(course, title)
        if existing is not None:
            # Same work, already known: a row still waiting for its first push,
            # or one whose task was deleted. Link it rather than creating a
            # duplicate. If it already points at a different task, leave both
            # alone — two tasks with one name is not something to guess at.
            if not existing["gtask_id"]:
                self.repo.set_gtask(int(existing["id"]), task["id"], tasklist_id)
                self.repo.mark_task_synced(int(existing["id"]))
            return None

        done = task.get("status") == "completed"
        assignment_id = self.repo.create_assignment(
            title,
            sheet_id=gsheets.new_sheet_id(),
            course=course,
            kind=guess_kind(title),
            due_at=gtasks.from_due(task.get("due")),
            percent_done=100.0 if done else 0.0,
        )
        self.repo.set_gtask(assignment_id, task["id"], tasklist_id)
        self.repo.mark_task_synced(assignment_id)
        if done:
            self.repo.set_status(assignment_id, "done", dirty=False)
        return assignment_id

    # --- seeding --------------------------------------------------------
    def seed_from_tasks(self) -> int:
        """First run: turn the tasks already in the account into sheet rows."""
        created = 0
        for tasklist_id, course in self._tasklists():
            for task in self.tasks.list_tasks(tasklist_id):
                if self._adopt_task(task, tasklist_id, course) is not None:
                    created += 1
        return created


KIND_HINTS = [
    ("exam", ("exam", "midterm", "final exam")),
    ("quiz", ("quiz",)),
    ("homework", ("hw", "homework", "problem set", "pset", "assignment")),
    ("project", ("project", "sprint", "demo", "milestone")),
    ("reading", ("read", "chapter", "week ")),
    ("writing", ("essay", "writeup", "paper", "report", "charter", "email")),
    ("presentation", ("presentation", "slides", "talk")),
]


def guess_kind(title: str) -> str | None:
    lowered = title.lower()
    for kind, hints in KIND_HINTS:
        if any(hint in lowered for hint in hints):
            return kind
    return None
