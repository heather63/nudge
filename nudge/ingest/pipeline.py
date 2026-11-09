"""Folder -> documents -> proposed estimate. Stops at the approval gate."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .. import timeutil as tu
from ..connectors import gsheets
from ..store.repo import Repo
from . import estimate as est
from . import extract

log = logging.getLogger(__name__)


def watch_root() -> Path:
    return Path(config.WATCH_FOLDER).expanduser()


@dataclass(slots=True)
class Ingested:
    proposal_id: int
    assignment_id: int
    label: str
    payload: dict


def folder_identity(folder: Path) -> tuple[str | None, str]:
    """~/assignments/<class>/<assignment> -> ("CS 4262", "HW1").

    A folder placed directly in the watch root has no class.
    """
    folder = folder.resolve()
    root = watch_root().resolve()
    course = folder.parent.name if folder.parent != root else None
    return course, folder.name


def ingest_folder(
    repo: Repo, folder: Path, *, client=None, force: bool = False
) -> Ingested | None:
    """Returns a proposal awaiting approval, or None when there is nothing new."""
    folder = Path(folder)
    if not folder.is_dir():
        raise est.EstimateError(f"{folder} is not a folder")

    course, name = folder_identity(folder)
    label = f"{course} · {name}" if course else name
    documents = extract.read_folder(folder)
    if not documents and not force:
        log.info("no readable documents in %s", folder)
        return None

    changed = force or any(
        repo.document_sha(str(path)) != sha for path, sha, _ in documents
    )
    if not changed:
        log.info("%s unchanged since last ingest", folder)
        return None

    row = repo.find_assignment(course, name)
    if row is None:
        # The sheet is the source of truth for what exists, so a new folder
        # creates a row rather than being ignored.
        assignment_id = repo.create_assignment(name, course=course)
        due_at = None
    else:
        assignment_id = int(row["id"])
        due_at = tu.parse(row["due_at"])

    result = est.estimate(
        course, name, due_at, [(p.name, text) for p, _, text in documents], client=client
    )

    for path, sha, text in documents:
        repo.upsert_document(assignment_id, str(path), sha, text)

    payload = {
        "expected_minutes": result.expected_minutes,
        "kind": result.kind,
        "due_date": result.due_date,
        "reasoning": result.reasoning,
        "files": ", ".join(str(p) for p, _, _ in documents),
    }
    proposal_id = repo.create_proposal(assignment_id, json.dumps(payload))
    return Ingested(proposal_id, assignment_id, label, payload)


def approve(repo: Repo, proposal_row) -> dict:
    """Write an approved estimate into the mirror; the sync pushes it onward."""
    payload = json.loads(proposal_row["payload_json"])
    assignment_id = int(proposal_row["assignment_id"])
    row = repo.get_assignment(assignment_id)

    fields: dict = {"expected_min": payload["expected_minutes"]}
    if payload.get("kind") and not row["kind"]:
        fields["kind"] = payload["kind"]
    if payload.get("files"):
        fields["files"] = payload["files"]
    if payload.get("due_date") and not row["due_at"]:
        parsed = gsheets.parse_date(payload["due_date"])
        if parsed:
            fields["due_at"] = parsed

    repo.update_assignment(assignment_id, **fields)
    repo.resolve_proposal(int(proposal_row["id"]), "approved")
    return fields
