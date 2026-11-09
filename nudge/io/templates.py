"""Nag phrasing.

Every message has a deterministic form. An LLM may later be plugged in via
`set_phraser` to rewrite an offer, but if it fails or is slow the template
below is what gets sent (plan §12).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from .. import config
from .. import timeutil as tu
from ..engine import capacity
from ..store.repo import Candidate

log = logging.getLogger(__name__)

Phraser = Callable[[Candidate, int, int], str]
_phraser: Phraser | None = None


def set_phraser(fn: Phraser | None) -> None:
    global _phraser
    _phraser = fn


# Variants are picked deterministically, so the same situation reads the same
# way twice — easier to tell "it nagged again" from "it nagged differently".
_LEVEL_1 = [
    "{gap} free. Put **{session}** into {name}?",
    "You've got {gap}. **{session}** on {name}?",
    "Gap of {gap} — worth **{session}** on {name}?",
]
# When the session fills the whole gap, naming both reads as a stutter
# ("You've got 1h25. 1h25 on ...").
_LEVEL_1_BOXED = [
    "**{session}** free. Spend it on {name}?",
    "Got **{session}**. Put it into {name}?",
    "**{session}** clear — {name}?",
]
_LEVEL_2 = [
    "**{session}** on {name}? {progress}, {due}.",
    "{name} is {due} and {progress}. **{session}** now?",
]
_LEVEL_4 = [
    "{name} again — turned down {declines} times running. {progress}, {due}. "
    "**{session}**?",
    "{declines} passes in a row on {name}. It's {due} and {progress}. "
    "**{session}** would move it.",
]
_LEVEL_6 = (
    "That's {declines} passes on {name}. I'll stop bringing it up — say `undo` "
    "if that's wrong, or set it to 100% when it's done."
)


def _pick(options: list[str], seed: int) -> str:
    return options[seed % len(options)]


def progress_phrase(candidate: Candidate) -> str:
    percent = int(round(candidate.percent_done))
    left = tu.fmt_duration(candidate.remaining_min)
    if percent <= 0:
        return f"{left} of work"
    return f"{percent}% done, {left} left"


def offer(
    candidate: Candidate,
    level: int,
    session_min: int,
    usable_gap_min: float,
    now: datetime | None = None,
) -> str:
    now = now or tu.now_utc()
    fields = {
        "name": f"**{candidate.label}**",
        "session": tu.fmt_duration(session_min),
        "gap": tu.fmt_duration(usable_gap_min),
        "due": tu.fmt_due(candidate.due_at, now=now),
        "progress": progress_phrase(candidate),
        "declines": candidate.decline_count,
    }

    if level >= config.MAX_ESCALATION:
        return _LEVEL_6.format(**fields)
    seed = candidate.assignment_id + candidate.offer_count
    boxed = session_min >= usable_gap_min - 1
    if level >= 4:
        text = _pick(_LEVEL_4, seed).format(**fields)
    elif level >= 2:
        text = _pick(_LEVEL_2, seed).format(**fields)
    else:
        text = _pick(_LEVEL_1_BOXED if boxed else _LEVEL_1, seed).format(**fields)

    if _phraser is not None:
        try:
            rewritten = _phraser(candidate, level, session_min)
            if rewritten and rewritten.strip():
                return rewritten.strip()
        except Exception:  # noqa: BLE001 - never let phrasing break the nag
            log.warning("phraser failed, using template", exc_info=True)
    return text


# --- acknowledgements ----------------------------------------------------
def ack_done(name: str) -> str:
    return f"**{name}** marked 100%. Sheet and Tasks updated."


def ack_session_done(name: str, session_min: int, percent: float,
                     remaining_min: float) -> str:
    """`done` on a slice of something bigger means the slice, not the lot."""
    return (
        f"Logged {tu.fmt_duration(session_min)} on **{name}** — "
        f"{int(round(percent))}%, {tu.fmt_duration(remaining_min)} left. "
        "Say `all done` if you meant the whole thing."
    )


def ack_progress(name: str, percent: float, remaining_min: float) -> str:
    if percent >= 100:
        return ack_done(name)
    return (
        f"**{name}** now {int(round(percent))}% — "
        f"{tu.fmt_duration(remaining_min)} left."
    )


def ack_not_now() -> str:
    return "Fine. Nothing else this gap."


def ack_snooze(name: str, until: datetime) -> str:
    when = f"{tu.to_local(until):%a %-I:%M%p}".lower()
    return f"**{name}** snoozed until {when}."


def ack_skip(name: str) -> str:
    return f"Won't bring up **{name}** again. It stays in the sheet; `undo` to reverse."


def ack_undo(name: str) -> str:
    return f"**{name}** is back in rotation."


def ack_busy(until: datetime, understood: bool) -> str:
    when = f"{tu.to_local(until):%a %-I:%M%p}".lower()
    if understood:
        return f"Understood — nothing until {when}."
    return (
        f"Didn't catch how long, so I'll leave you alone until {when}. "
        "Try `busy until friday` or `busy for 3h`."
    )


def ack_mute(until: datetime) -> str:
    when = f"{tu.to_local(until):%a %-I:%M%p}".lower()
    return f"Quiet until {when}."


def ack_unmute() -> str:
    return "Unmuted."


def needs_expected_length(name: str) -> str:
    return (
        f"**{name}** has no expected length, so I can't turn time into progress. "
        f"Set one in the sheet, or reply with a percentage like `40%`."
    )


def no_open_offer() -> str:
    return "No offer outstanding. `status` shows what's active."


def completed_elsewhere(names: list[str]) -> str:
    joined = ", ".join(f"**{n}**" for n in names)
    return f"Marked done in Google Tasks: {joined}."


def reopened_elsewhere(names: list[str]) -> str:
    joined = ", ".join(f"**{n}**" for n in names)
    return (
        f"{joined} was reopened in Google Tasks but the sheet still says 100%. "
        "Reply with a percentage to set where it actually is."
    )


def new_from_sheet(names: list[str]) -> str:
    joined = ", ".join(f"**{n}**" for n in names)
    return f"Picked up from the sheet: {joined}."


def adopted_from_tasks(names: list[str]) -> str:
    joined = ", ".join(f"**{n}**" for n in names)
    return (
        f"Added to the sheet from Google Tasks: {joined}. "
        "Set an expected length so I can size a sitting."
    )


def auth_problem(message: str) -> str:
    return f"Google auth is broken, so the sheet is out of sync.\n{message}"


# --- proposals (expected length from documents) --------------------------
def proposal(name: str, payload: dict) -> str:
    lines = [f"**{name}** — read the documents you dropped in:"]
    if payload.get("expected_minutes"):
        lines.append(f"- Expected length: **{tu.fmt_duration(payload['expected_minutes'])}**")
    if payload.get("kind"):
        lines.append(f"- Type: **{payload['kind']}**")
    if payload.get("due_date"):
        lines.append(f"- Due: **{payload['due_date']}**")
    if payload.get("reasoning"):
        lines.append(f"_{payload['reasoning']}_")
    lines.append("")
    lines.append("`approve` to write that to the sheet, or `reject`.")
    return "\n".join(lines)


def ack_approved(name: str) -> str:
    return f"Written to the sheet for **{name}**."


def ack_rejected(name: str) -> str:
    return f"Dropped the estimate for **{name}**. The sheet is unchanged."


def nothing_to_approve() -> str:
    return "No proposal outstanding."


def ingest_failed(name: str, message: str) -> str:
    return f"Couldn't read the documents for **{name}**.\n{message}"


# --- capacity and status -------------------------------------------------
def capacity_warning(warnings: list, now: datetime | None = None) -> str:
    """The one thing the agent says that you did not already know (plan §9)."""
    now = now or tu.now_utc()
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


def headroom(
    remaining_min: float,
    due_at: datetime | None,
    now: datetime,
    available_min: float | None = None,
) -> str:
    if due_at is None:
        return ""
    slack = capacity.headroom_minutes(remaining_min, now, due_at, available_min)
    if slack == float("inf"):
        return ""
    if slack < 0:
        return f" — **{tu.fmt_duration(-slack)} short**"
    return f" ({tu.fmt_duration(slack)} spare)"


def status(rows, now: datetime | None = None, availability: dict | None = None,
           limit: int = 12) -> str:
    from ..store.repo import remaining_minutes

    now = now or tu.now_utc()
    if not rows:
        return "Nothing active."
    lines = ["**Active**"]
    for row in rows[:limit]:
        remaining = remaining_minutes(row["expected_min"], row["percent_done"])
        due = tu.fmt_due(tu.parse(row["due_at"]), now=now)
        percent = int(round(row["percent_done"] or 0))
        name = f"{row['course']} · {row['name']}" if row["course"] else row["name"]
        slack = headroom(
            remaining,
            tu.parse(row["due_at"]),
            now,
            (availability or {}).get(int(row["id"])),
        )
        opens = tu.parse(row["available_from"])
        gate = ""
        if opens is not None and opens > now:
            when = f"{tu.to_local(opens):%a %-I%p}".lower()
            gate = f" — _opens {when}_"
        lines.append(
            f"- {name} — {percent}%, {tu.fmt_duration(remaining)} left, "
            f"{due}{slack}{gate}"
        )
    if len(rows) > limit:
        lines.append(f"_...and {len(rows) - limit} more_")
    return "\n".join(lines)


HELP = """\
`done` finished the sitting I asked for (or the assignment, if that was all)
`all done` the whole assignment is finished
`did 40` / `did 1h` log time you actually spent
`40%` set completion directly
`not now` decline; nothing else this gap
`snooze 2h` / `snooze tomorrow` push it out
`skip` stop offering it (the sheet row stays)
`undo` reverse the last skip
`something else` next-ranked assignment
`busy until friday` / `busy for 3h` stop nagging until then
`mute 3h` / `unmute` global silence
`status` what's active
`sync` force a sheet + Tasks sync
`capacity` does the remaining work still fit?
`sheet` link to the spreadsheet
`approve` / `reject` answer a proposed estimate
`ingest CS 4262/HW1` re-read that assignment folder
`gap 30` force a 30-minute gap (debug)
`why` score breakdown for the last ranking (debug)\
"""
