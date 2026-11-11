"""Everything nudge says. Deterministic, so the same situation reads the same way."""
from __future__ import annotations

from datetime import datetime

from .. import config
from .. import timeutil as tu
from ..engine import capacity
from ..sheet import Assignment

_OFFER = [
    "You've got {gap}. **{session}** on {name}? {progress}, {due}.",
    "{gap} free. Put **{session}** into {name}? {progress}, {due}.",
]
# When the session fills the whole gap, naming both reads as a stutter.
_OFFER_BOXED = [
    "**{session}** free. Spend it on {name}? {progress}, {due}.",
    "Got **{session}**. Put it into {name}? {progress}, {due}.",
]


def progress_phrase(a: Assignment) -> str:
    percent = int(round(a.percent_done))
    left = tu.fmt_duration(a.remaining_min)
    if percent <= 0:
        return f"{left} of work"
    return f"{percent}% done, {left} left"


def offer(a: Assignment, session_min: int, usable_gap_min: float, now: datetime,
          seed: int = 0) -> str:
    boxed = session_min >= usable_gap_min - 1
    options = _OFFER_BOXED if boxed else _OFFER
    return options[seed % len(options)].format(
        name=f"**{a.label}**",
        session=tu.fmt_duration(session_min),
        gap=tu.fmt_duration(usable_gap_min),
        due=tu.fmt_due(a.due_at, now=now),
        progress=progress_phrase(a),
    )


# --- replies -------------------------------------------------------------
def ack_done(label: str) -> str:
    return f"**{label}** marked 100% in the sheet."


def ack_session_done(label: str, session_min: int, percent: float,
                     remaining_min: float) -> str:
    """"done" on a slice of something bigger means the slice, not the lot."""
    return (
        f"Logged {tu.fmt_duration(session_min)} on **{label}** — "
        f"{int(round(percent))}%, {tu.fmt_duration(remaining_min)} left. "
        "Reply `100%` if you finished the whole thing."
    )


def ack_progress(label: str, percent: float, remaining_min: float) -> str:
    if percent >= 100:
        return ack_done(label)
    return (
        f"**{label}** now {int(round(percent))}% — "
        f"{tu.fmt_duration(remaining_min)} left."
    )


def ack_not_now() -> str:
    return "Fine. Nothing else this gap."


def ack_set_aside(label: str, until: datetime) -> str:
    return (
        f"That's {config.MAX_DECLINES} passes in a row on **{label}**. "
        f"I'll leave it alone until {tu.fmt_when(until)}."
    )


def ack_snooze(label: str, until: datetime) -> str:
    return f"**{label}** snoozed until {tu.fmt_when(until)}."


def ack_busy(until: datetime, understood: bool) -> str:
    if understood:
        return f"Understood — nothing until {tu.fmt_when(until)}. `busy off` to undo."
    return (
        f"Didn't catch how long, so I'll leave you alone until {tu.fmt_when(until)}. "
        "Try `busy until friday` or `busy for 3h`."
    )


def ack_back() -> str:
    return "Back on."


def needs_expected_length(label: str) -> str:
    return (
        f"**{label}** has no expected length, so I can't turn time into progress. "
        "Set one in the sheet, or reply with a percentage like `40%`."
    )


def no_open_offer() -> str:
    return "No offer outstanding. `status` shows what's active."


def row_missing(label: str) -> str:
    return f"**{label}** isn't in the sheet any more, so there's nothing to update."


def auth_problem(message: str) -> str:
    return f"Google auth is broken, so I can't read the sheet or calendar.\n{message}"


# --- capacity and status -------------------------------------------------
def capacity_warning(warnings: list, now: datetime) -> str:
    """The one thing nudge says that you did not already know."""
    if len(warnings) == 1:
        w = warnings[0]
        return (
            f"**{w.name}** no longer fits. {tu.fmt_duration(w.remaining_min)} of work "
            f"left, about {tu.fmt_duration(w.usable_min)} of realistic time before "
            f"it's {tu.fmt_due(w.due_at, now=now)} — "
            f"**{tu.fmt_duration(w.short_by)} short**."
        )
    lines = ["These no longer fit in the time left:"]
    for w in warnings:
        lines.append(
            f"- **{w.name}** — {tu.fmt_duration(w.remaining_min)} of work, "
            f"{tu.fmt_duration(w.usable_min)} available, "
            f"{tu.fmt_duration(w.short_by)} short ({tu.fmt_due(w.due_at, now=now)})"
        )
    return "\n".join(lines)


def _headroom(a: Assignment, now: datetime, available_min: float | None) -> str:
    if a.due_at is None:
        return ""
    slack = capacity.headroom_minutes(a.remaining_min, now, a.due_at, available_min)
    if slack == float("inf"):
        return ""
    if slack < 0:
        return f" — **{tu.fmt_duration(-slack)} short**"
    return f" ({tu.fmt_duration(slack)} spare)"


def status(assignments: list[Assignment], now: datetime,
           availability: dict[str, float] | None = None, limit: int = 12) -> str:
    active = [a for a in assignments if not a.done]
    if not active:
        return "Nothing active."
    far_future = datetime.max.replace(tzinfo=now.tzinfo)
    active.sort(key=lambda a: a.due_at or far_future)
    lines = ["**Active**"]
    for a in active[:limit]:
        gate = ""
        if a.available_from and a.available_from > now:
            gate = f" — _opens {tu.fmt_when(a.available_from)}_"
        lines.append(
            f"- {a.label} — {int(round(a.percent_done))}%, "
            f"{tu.fmt_duration(a.remaining_min)} left, {tu.fmt_due(a.due_at, now=now)}"
            f"{_headroom(a, now, (availability or {}).get(a.sheet_id))}{gate}"
        )
    if len(active) > limit:
        lines.append(f"_...and {len(active) - limit} more_")
    return "\n".join(lines)


HELP = """\
Use the buttons under an offer, or reply:
`done` finished the sitting I asked for (or the assignment, if that was all)
`did 40` / `did 1h` log time you actually spent
`40%` set completion directly (`100%` = finished)
`not now` decline; nothing else this gap
`something else` the next-best assignment
`snooze tomorrow` / `snooze friday` push this assignment out
`busy until 5pm` / `busy for 3h` nothing at all until then (`busy off` to undo)
`status` what's active\
"""
