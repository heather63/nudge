"""Typed DM replies. Most of the time you'll press a button instead.

Case-insensitive and tolerant of extra words: "ok done, finally" lands. Kept
separate from bot.py so it is testable without Discord.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    arg: str = ""


_UNITS = r"(?:h|hr|hrs|hours?|m|min|mins|minutes?)"

# Ordered: the first pattern that matches anywhere in the message wins.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Progress first — "did 30" must not be read as "done".
    ("set_percent", re.compile(r"(?P<arg>\d{1,3})\s*%", re.I)),
    ("log_minutes", re.compile(
        rf"\b(?:did|spent|logged|worked|put in)\s+(?P<arg>\d+(?:\.\d+)?\s*{_UNITS}?)", re.I)),
    ("log_minutes", re.compile(rf"^\s*(?P<arg>\d+(?:\.\d+)?\s*{_UNITS})\s*$", re.I)),
    # "busy until friday" is about you, not the assignment, so it wins over "not now".
    ("busy", re.compile(r"\b(?:busy|swamped|tied up|in class)\b\s*(?P<arg>.*)", re.I)),
    ("snooze", re.compile(r"\bsnooze\b\s*(?P<arg>.*)", re.I)),
    ("something_else", re.compile(r"\b(something else|anything else|different one)\b", re.I)),
    # Before "done", which would otherwise swallow "not done yet".
    ("not_now", re.compile(r"\b(not now|not yet|not done|not finished|later|no thanks|nah|nope)\b", re.I)),
    ("done", re.compile(r"\b(done|did it|finished|completed?)\b", re.I)),
    ("status", re.compile(r"\b(status|what'?s left)\b", re.I)),
    ("help", re.compile(r"^\s*(help|commands|\?)\s*$", re.I)),
]


def parse(text: str) -> Command | None:
    text = (text or "").strip()
    if not text:
        return None
    for name, pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            arg = (match.group("arg") or "").strip() if "arg" in pattern.groupindex else ""
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
