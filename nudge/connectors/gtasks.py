"""Google Tasks client.

Tasks is a mirror of the sheet, one task per assignment. Due dates are
date-only on write, so all time-of-day arithmetic stays in SQLite.
"""
from __future__ import annotations

import logging
from datetime import datetime

from googleapiclient.errors import HttpError

from .. import config
from .. import timeutil as tu
from . import google_auth

log = logging.getLogger(__name__)

SYNC_CURSOR = "tasks_sync_cursor"  # one cursor per list: "<key>:<tasklist id>"

def rfc3339(dt: datetime) -> str:
    """Google wants a Z-suffixed timestamp; isoformat's +00:00 is riskier."""
    return dt.astimezone(tu.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def to_due(due_at: datetime | None) -> str | None:
    """Tasks truncates the time component, so send the local date at UTC midnight."""
    if due_at is None:
        return None
    return f"{tu.to_local(due_at).date().isoformat()}T00:00:00.000Z"


def from_due(value: str | None) -> datetime | None:
    """Read back the date only — the time Google returns is meaningless."""
    if not value:
        return None
    day = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    return datetime.combine(day, datetime.min.time(), config.TZ).astimezone(tu.UTC)


class TasksClient:
    """Thin wrapper: every call goes through the shared retry/backoff helper."""

    def __init__(self, service=None) -> None:
        self._service = service

    @property
    def service(self):
        if self._service is None:
            self._service = google_auth.build_service("tasks", "v1")
        return self._service

    def list_tasklists(self) -> list[dict]:
        response = google_auth.execute(self.service.tasklists().list(maxResults=1000))
        return response.get("items", [])

    def ensure_tasklist(self, title: str) -> str:
        for item in self.list_tasklists():
            if item["title"] == title:
                return item["id"]
        created = google_auth.execute(
            self.service.tasklists().insert(body={"title": title})
        )
        log.info("created Google task list %r", title)
        return created["id"]

    def list_tasks(self, tasklist_id: str, updated_min: str | None = None) -> list[dict]:
        """Delta sync. showCompleted+showHidden or completions vanish (plan §4.3)."""
        tasks: list[dict] = []
        page_token = None
        while True:
            request = self.service.tasks().list(
                tasklist=tasklist_id,
                showCompleted=True,
                showHidden=True,
                showDeleted=True,
                maxResults=100,
                updatedMin=updated_min,
                pageToken=page_token,
            )
            response = google_auth.execute(request)
            tasks.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return tasks

    def get(self, tasklist_id: str, task_id: str) -> dict | None:
        try:
            return google_auth.execute(
                self.service.tasks().get(tasklist=tasklist_id, task=task_id)
            )
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                return None
            raise

    def insert(
        self,
        tasklist_id: str,
        title: str,
        *,
        notes: str | None = None,
        due: str | None = None,
        parent: str | None = None,
    ) -> dict:
        body = {"title": title}
        if notes:
            body["notes"] = notes
        if due:
            body["due"] = due
        return google_auth.execute(
            self.service.tasks().insert(tasklist=tasklist_id, body=body, parent=parent)
        )

    def patch(self, tasklist_id: str, task_id: str, body: dict) -> dict:
        return google_auth.execute(
            self.service.tasks().patch(tasklist=tasklist_id, task=task_id, body=body)
        )

    def delete(self, tasklist_id: str, task_id: str) -> None:
        try:
            google_auth.execute(
                self.service.tasks().delete(tasklist=tasklist_id, task=task_id)
            )
        except HttpError as exc:
            if getattr(exc.resp, "status", None) != 404:
                raise
