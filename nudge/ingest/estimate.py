"""LLM estimation of an assignment from its documents.

The sheet needs an expected length for every row, and typing one for forty
assignments is the kind of chore that kills a system before it starts. So the
documents get read once and an estimate is proposed — behind an approval gate,
because a wrong number silently distorts every ranking and capacity warning.

The LLM never decides *when* anything happens.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .. import config
from .. import timeutil as tu

log = logging.getLogger(__name__)

KINDS = ("homework", "reading", "writing", "project", "exam", "quiz",
         "presentation", "admin")


class EstimateError(Exception):
    """Anything that stops a proposal being produced. Worth a DM, not a crash."""


class Estimate(BaseModel):
    expected_minutes: int = Field(
        description="Total focused minutes for a competent student, start to submit"
    )
    kind: Literal[KINDS] | None = Field(description="What sort of work this is")
    due_date: str | None = Field(
        description="YYYY-MM-DD if the documents state a deadline, else null"
    )
    reasoning: str = Field(description="One sentence on what drove the number")


SYSTEM = """\
You size a student's assignment from its own documents.

- expected_minutes is the TOTAL focused time from a standing start to submitting,
  including reading the spec, setup, the work itself, and a final pass.
- Estimate for a competent student who has done the reading, not a expert and not
  someone starting from nothing.
- Count only what the documents actually ask for. Do not invent deliverables.
- A problem set is usually 2-5 hours. A term project is tens of hours. A reading
  is usually under two. Say what the documents support, not a safe average.
- If the documents are too thin to size properly, give your best guess and say so
  plainly in `reasoning`.
"""


def _client():
    try:
        import anthropic

        return anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001
        raise EstimateError(
            "No Anthropic credentials. Set ANTHROPIC_API_KEY in .env "
            f"({type(exc).__name__})."
        ) from exc


def build_prompt(
    course: str | None,
    name: str,
    due_at: datetime | None,
    documents: list[tuple[str, str]],
) -> str:
    header = [f"Assignment: {name}"]
    if course:
        header.append(f"Course: {course}")
    if due_at:
        header.append(f"Known due date: {tu.to_local(due_at):%Y-%m-%d}")
    header.append(f"Today: {tu.to_local(tu.now_utc()):%Y-%m-%d}")

    body = []
    for doc_name, text in documents:
        body.append(f"\n--- {doc_name} ---\n{text}")
    if not body:
        body.append("\n(No readable documents — size it from the title alone.)")
    return "\n".join(header) + "\n" + "".join(body)


def estimate(
    course: str | None,
    name: str,
    due_at: datetime | None,
    documents: list[tuple[str, str]],
    *,
    client=None,
) -> Estimate:
    client = client or _client()
    prompt = build_prompt(course, name, due_at, documents)
    try:
        response = client.messages.parse(
            model=config.ESTIMATE_MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=Estimate,
        )
    except Exception as exc:  # noqa: BLE001
        raise EstimateError(f"{type(exc).__name__}: {exc}") from exc

    if response.stop_reason == "refusal":
        raise EstimateError("The model declined to size this one.")
    result = response.parsed_output
    if result is None:
        raise EstimateError("The model returned nothing parseable.")
    return sanitise(result)


def sanitise(result: Estimate) -> Estimate:
    """Clamp anything the model got carried away with."""
    result.expected_minutes = max(
        config.MIN_EXPECTED_MINUTES,
        min(int(result.expected_minutes), config.MAX_EXPECTED_MINUTES),
    )
    return result
