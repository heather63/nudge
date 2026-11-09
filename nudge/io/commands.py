"""DM reply parsing (plan §7).

Case-insensitive, tolerant of extra words. Kept separate from bot.py so the
whole command surface is testable without a Discord connection.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    arg: str = ""


_UNITS = r"(?:h|hr|hrs|hours?|m|min|mins|minutes?)"

# Ordered: the first pattern that matches anywhere in the message wins, so
# "ok done, that took a while" and "yeah not now" both land correctly.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Progress first — "did 30" must not be read as "done".
    ("set_percent", re.compile(r"(?P<arg>\d{1,3})\s*%", re.I)),
    ("log_minutes", re.compile(
        rf"\b(?:did|spent|logged|got|worked|put in)\s+(?P<arg>\d+\s*{_UNITS}?)", re.I)),
    ("log_minutes", re.compile(rf"^\s*(?P<arg>\d+\s*{_UNITS})\s*$", re.I)),
    # Proposal replies are unambiguous and must not be shadowed.
    ("approve", re.compile(r"\b(approve|approved|looks good|lgtm)\b", re.I)),
    ("reject", re.compile(r"\b(reject|discard|scrap (it|that))\b", re.I)),
    ("ingest", re.compile(r"\bingest\b\s*(?P<arg>.*)", re.I)),
    # "busy until friday" is a decline *and* a mute, so it must win over
    # "not now" and over the bare clock/duration patterns.
    ("busy", re.compile(
        r"\b(?:busy|swamped|tied up|in class|slammed|out)\b\s*(?P<arg>.*)", re.I)),
    ("not_now", re.compile(r"\b(not now|later|nope|no thanks|nah|pass)\b", re.I)),
    ("something_else", re.compile(r"\b(something else|next|other|different)\b", re.I)),
    ("snooze", re.compile(r"\bsnooze\b\s*(?P<arg>.*)", re.I)),
    ("unmute", re.compile(r"\b(unmute|unsilence|back on)\b", re.I)),
    ("mute", re.compile(r"\b(mute|shush|quiet)\b\s*(?P<arg>.*)", re.I)),
    ("undo", re.compile(r"\b(undo|unskip|nevermind|never mind)\b", re.I)),
    ("skip", re.compile(r"\b(skip|drop|forget it)\b", re.I)),
    # Must precede "done", which would otherwise swallow "all done".
    ("finish_all", re.compile(
        r"\b(all done|fully done|completely done|finished it|"
        r"done with (?:it|the whole thing))\b", re.I)),
    ("done", re.compile(r"\b(done|did it|finished|complete[d]?|yep|got it)\b", re.I)),
    ("sheet", re.compile(r"\b(sheet|spreadsheet)\b", re.I)),
    ("status", re.compile(r"\b(status|what'?s left|pile)\b", re.I)),
    ("sync", re.compile(r"\b(sync|refresh)\b", re.I)),
    ("capacity", re.compile(r"\b(capacity|will it fit|does it fit)\b", re.I)),
    ("why", re.compile(r"\bwhy\b", re.I)),
    ("help", re.compile(r"\b(help|commands|\?)\b", re.I)),
    ("gap", re.compile(r"\bgap\b\s*(?P<arg>.*)", re.I)),
]


def parse(text: str) -> Command | None:
    text = (text or "").strip()
    if not text:
        return None
    for name, pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            arg = ""
            if "arg" in pattern.groupindex:
                arg = (match.group("arg") or "").strip()
            return Command(name, arg)
    return None


_WORKED = re.compile(rf"^\s*(?P<n>\d+(?:\.\d+)?)\s*(?P<unit>{_UNITS})?\s*$", re.I)


def parse_worked_minutes(arg: str) -> float | None:
    """'30' -> 30 minutes. Bare numbers here mean minutes, not hours: nobody
    replies to "spend 45 min on this" with "did 2" meaning two hours."""
    match = _WORKED.match(arg or "")
    if not match:
        return None
    amount = float(match.group("n"))
    unit = (match.group("unit") or "m").lower()
    return amount * 60 if unit.startswith("h") else amount
