"""Gap detection (plan §5).

`invert_busy` and `Gap` are already in their phase-3 shape; only the source of
busy intervals changes when connectors/gcal.py arrives.
"""
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
    source: str = "calendar"

    @property
    def length_min(self) -> float:
        return tu.minutes_between(self.start, self.end)

    def usable_min(self, now: datetime) -> float:
        """Time actually available to spend, from now, minus the tail buffer.

        Capped: a free afternoon is not a five-hour work session, and an
        uncapped gap makes `fit` return 1.0 for everything (see config).
        """
        remaining = tu.minutes_between(now, self.end) - config.TAIL_BUFFER
        return min(remaining, float(config.MAX_USABLE_GAP_MINUTES))

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def key(self) -> str:
        return f"{tu.iso(self.start)}|{tu.iso(self.end)}"


def invert_busy(
    busy: Iterable[tuple[datetime, datetime]], window_start: datetime, window_end: datetime
) -> list[Gap]:
    """Turn freeBusy intervals into free intervals, clipped to the window."""
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


def _local_midnight(now: datetime | None = None) -> datetime:
    local = tu.to_local(now or tu.now_utc())
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(tu.UTC)


class GapSource(Protocol):
    def current_gap(self, now: datetime) -> Gap | None: ...

    def refresh(self, now: datetime) -> None:
        """May block on the network. The engine calls it off the event loop."""


class FakeGapSource:
    """Phase 1 stand-in: a synthetic gap opens on a fixed cadence.

    Deterministic on purpose — the schedule has to stay debuggable (plan §12).
    Lengths cycle through FAKE_GAP_LENGTHS so every gap size gets exercised.
    """

    def __init__(
        self,
        every_min: int = config.FAKE_GAP_EVERY_MIN,
        lengths: tuple[int, ...] = config.FAKE_GAP_LENGTHS,
        epoch: datetime | None = None,
    ) -> None:
        self.every_min = every_min
        self.lengths = lengths
        # Anchored to local midnight, not process start, so restarting the bot
        # does not reset the synthetic schedule.
        self.epoch = epoch or _local_midnight()
        self._injected: Gap | None = None

    def inject(self, minutes: int, now: datetime | None = None) -> Gap:
        """`gap 30` from a DM: "I have 30 free minutes starting now".

        The start is backdated past LEAD_IN_DELAY so the injected gap is
        immediately usable; the end is `minutes` out, so the usable window is
        `minutes - TAIL_BUFFER`, exactly as a real gap would be.
        """
        now = now or tu.now_utc()
        start = now - timedelta(minutes=config.LEAD_IN_DELAY + 1)
        self._injected = Gap(start, now + timedelta(minutes=minutes), source="injected")
        return self._injected

    def refresh(self, now: datetime) -> None:
        """Nothing to fetch — the fake schedule is pure arithmetic."""

    def current_gap(self, now: datetime) -> Gap | None:
        if self._injected:
            if self._injected.end > now:
                return self._injected
            self._injected = None

        elapsed = tu.minutes_between(self.epoch, now)
        if elapsed < 0:
            return None
        index = int(elapsed // self.every_min)
        start = self.epoch + timedelta(minutes=index * self.every_min)
        length = self.lengths[index % len(self.lengths)]
        gap = Gap(start, start + timedelta(minutes=length), source="fake")
        return gap if gap.end > now else None


class CalendarGapSource:
    """Real gaps: freeBusy inverted into free intervals (plan §5).

    `refresh` does the network call and is expected to run off the event loop;
    `current_gap` only reads the cache, so a tick never blocks on Google.
    """

    def __init__(
        self,
        client=None,
        window_hours: int = config.FREEBUSY_WINDOW_HOURS,
        cache_seconds: int = config.FREEBUSY_CACHE_SECONDS,
    ) -> None:
        self._client = client
        self.window_hours = window_hours
        self.cache_seconds = cache_seconds
        self._gaps: list[Gap] = []
        self._fetched_at: datetime | None = None
        self._window_end: datetime | None = None

    @property
    def client(self):
        if self._client is None:
            from ..connectors.gcal import CalendarClient

            self._client = CalendarClient()
        return self._client

    def _stale(self, now: datetime) -> bool:
        if self._fetched_at is None or self._window_end is None:
            return True
        if tu.minutes_between(self._fetched_at, now) * 60 >= self.cache_seconds:
            return True
        return now >= self._window_end

    def refresh(self, now: datetime, *, force: bool = False) -> None:
        if not force and not self._stale(now):
            return
        # Start the window in the past so the gap you are already sitting in
        # keeps its true start; see FREEBUSY_LOOKBACK_MINUTES.
        window_start = now - timedelta(minutes=config.FREEBUSY_LOOKBACK_MINUTES)
        window_end = now + timedelta(hours=self.window_hours)
        busy = self.client.busy(window_start, window_end)
        self._gaps = invert_busy(busy, window_start, window_end)
        self._fetched_at = now
        self._window_end = window_end

    def free_intervals(
        self, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Free time over an arbitrary range — used by the daily capacity check."""
        busy = self.client.busy(start, end)
        return [(gap.start, gap.end) for gap in invert_busy(busy, start, end)]

    def current_gap(self, now: datetime) -> Gap | None:
        for gap in self._gaps:
            if gap.contains(now):
                return gap
        return None
