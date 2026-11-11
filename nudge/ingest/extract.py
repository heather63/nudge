"""Assignment documents to plain text, for `nudge size`."""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

SUPPORTED = {".pdf", ".docx", ".md", ".markdown", ".txt"}
MAX_CHARS_PER_DOC = 60_000  # a spec longer than this is padding, not content


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


def read_documents(path: Path) -> list[tuple[str, str]]:
    """(file name, text) for a single document, or every supported one in a folder."""
    paths = [path] if path.is_file() else sorted(p for p in path.iterdir() if p.is_file())
    results = []
    for item in paths:
        if not is_supported(item):
            continue
        try:
            results.append((item.name, extract(item)))
        except Exception:  # noqa: BLE001 - one bad PDF must not stop the rest
            log.exception("could not extract %s", item)
    return results
