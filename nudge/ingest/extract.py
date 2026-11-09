"""Assignment documents to plain text. Context for decomposition only.

File activity is never a completion signal (plan §1) — these documents exist
solely so the LLM has something to break down.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

log = logging.getLogger(__name__)

SUPPORTED = {".pdf", ".docx", ".md", ".markdown", ".txt"}
MAX_CHARS_PER_DOC = 60_000  # a spec longer than this is padding, not content


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED and not path.name.startswith((".", "~$"))


def extract(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = _extract_pdf(path)
    elif suffix == ".docx":
        text = _extract_docx(path)
    else:
        text = path.read_text(errors="replace")
    text = text.strip()
    if len(text) > MAX_CHARS_PER_DOC:
        log.warning("%s truncated to %d chars", path.name, MAX_CHARS_PER_DOC)
        text = text[:MAX_CHARS_PER_DOC]
    return text


def _extract_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def read_folder(folder: Path) -> list[tuple[Path, str, str]]:
    """(path, sha256, text) for every supported document in an assignment folder."""
    results = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or not is_supported(path):
            continue
        try:
            results.append((path, sha256(path), extract(path)))
        except Exception:  # noqa: BLE001 - one bad PDF must not stop the rest
            log.exception("could not extract %s", path)
    return results
