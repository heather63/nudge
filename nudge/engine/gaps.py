"""Free time, from Google Calendar's freeBusy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Protocol

from .. import config
from .. import timeutil as tu


@dataclass(frozen=True, slots=True)
class Gap:
    start: datetime
    end: datetime

    def usable_min(self, now: datetime) -> float:
        """Time you can actually spend from now: minus a buffer, capped at one sitting."""
        remaining = tu.minutes_between(now, self.end) - config.TAIL_BUFFER
        return min(remaining, float(config.MAX_SESSION_MINUTES))

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


def invert_busy(
    busy: Iterable[tuple[datetime, datetime]], window_start: datetime, window_end: datetime
) -> list[Gap]:
    """Turn busy intervals into free ones, clipped to the window."""
    merged: list[list[datetime]] = []
    clipped = sorted(
        (max(s, window_start), min(e, window_end))
        for s, e in busy
        if e > window_start and s < window_end
    )
    for start, end in clipped:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    gaps: list[Gap] = []
    cursor = window_start
    for start, end in merged:
        if start > cursor:
            gaps.append(Gap(cursor, start))
        cursor = max(cursor, end)
    if cursor < window_end:
        gaps.append(Gap(cursor, window_end))
    return gaps


class GapSource(Protocol):
    def refresh(self, now: datetime) -> None:
        """May block on the network. The engine calls it off the event loop."""

    def current_gap(self, now: datetime) -> Gap | None: ...

    def free_intervals(
        self, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]: ...


class CalendarGapSource:
    """`refresh` does the network call; `current_gap` only reads what it fetched."""

    def __init__(self, client=None) -> None:
        self._client = client
        self._gaps: list[Gap] = []

    @property
    def client(self):
        if self._client is None:
            from ..connectors.gcal import CalendarClient

            self._client = CalendarClient()
        return self._client

    def refresh(self, now: datetime) -> None:
        end = now + timedelta(hours=config.FREEBUSY_WINDOW_HOURS)
        self._gaps = invert_busy(self.client.busy(now, end), now, end)

    def current_gap(self, now: datetime) -> Gap | None:
        return next((gap for gap in self._gaps if gap.contains(now)), None)

    def upcoming(self, now: datetime) -> list[Gap]:
        return [gap for gap in self._gaps if gap.end > now]

    def free_intervals(
        self, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Free time over any range — used by the daily capacity check."""
        busy = self.client.busy(start, end)
        return [(gap.start, gap.end) for gap in invert_busy(busy, start, end)]
