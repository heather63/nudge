"""Google Calendar freeBusy. Read-only — nudge never writes to a calendar."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .. import config
from .. import timeutil as tu
from . import google_auth

log = logging.getLogger(__name__)

MAX_ITEMS_PER_QUERY = 50  # freeBusy caps the items list


class CalendarClient:
    def __init__(self, service=None) -> None:
        self._service = service
        self._calendars: list[dict] | None = None
        # Calendars that failed once are not asked again this run. A subscribed
        # holiday calendar 404s on freeBusy and would otherwise fail every tick.
        self._broken: set[str] = set()

    @property
    def service(self):
        if self._service is None:
            self._service = google_auth.build_service("calendar", "v3")
        return self._service

    def list_calendars(self) -> list[dict]:
        if self._calendars is None:
            response = google_auth.execute(
                self.service.calendarList().list(maxResults=250)
            )
            self._calendars = response.get("items", [])
        return self._calendars

    def watched_calendars(self) -> list[tuple[str, str]]:
        """(id, name) for the calendars that count as "busy"."""
        watched = []
        for item in self.list_calendars():
            name = item.get("summary") or item["id"]
            if config.CALENDARS is not None and name not in config.CALENDARS:
                continue
            if name in config.CALENDAR_EXCLUDE or item["id"] in self._broken:
                continue
            watched.append((item["id"], name))
        return watched

    def busy(self, time_min: datetime, time_max: datetime) -> list[tuple[datetime, datetime]]:
        """Busy intervals across every watched calendar, unmerged.

        A calendar that errors is dropped from this and future queries rather
        than taking the whole tick down with it.
        """
        calendars = self.watched_calendars()
        if not calendars:
            return []
        names = dict(calendars)

        intervals: list[tuple[datetime, datetime]] = []
        ids = [cid for cid, _ in calendars]
        for start in range(0, len(ids), MAX_ITEMS_PER_QUERY):
            batch = ids[start : start + MAX_ITEMS_PER_QUERY]
            body = {
                "timeMin": time_min.astimezone(tu.UTC).isoformat(),
                "timeMax": time_max.astimezone(tu.UTC).isoformat(),
                "items": [{"id": cid} for cid in batch],
            }
            response = google_auth.execute(self.service.freebusy().query(body=body))
            for cid, data in response.get("calendars", {}).items():
                errors = data.get("errors")
                if errors:
                    reason = errors[0].get("reason", "unknown")
                    log.warning(
                        "calendar %r unreadable (%s) — skipping it from now on",
                        names.get(cid, cid),
                        reason,
                    )
                    self._broken.add(cid)
                    continue
                for block in data.get("busy", []):
                    intervals.append((tu.parse(block["start"]), tu.parse(block["end"])))
        return intervals
