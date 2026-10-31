"""The books folder: find files, keep the documents table in sync, parse books into sections."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from chapterwise import jobs
from chapterwise.db import DocumentRow, SectionRow
from chapterwise.models import Document, Section
from chapterwise.parsers import SUPPORTED_EXTENSIONS, ParseError, parse
from chapterwise.summarize import text_sha256

log = logging.getLogger(__name__)

FORMATS = {".epub": "epub", ".pdf": "pdf", ".md": "md", ".markdown": "md"}


@dataclass
class ScanResult:
    added: int = 0
    changed: int = 0
    missing: int = 0
    unchanged: int = 0


def find_books(books_dir: Path) -> dict[str, tuple[int, float]]:
    """Supported files under books_dir as {relative path: (size, mtime)}. Hidden folders are skipped."""
    if not books_dir.is_dir():
        raise FileNotFoundError(f"Books folder not found: {books_dir}")
    found: dict[str, tuple[int, float]] = {}
    for root, dirs, files in os.walk(books_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith(".") or Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            path = Path(root, name)
            try:
                stat = path.stat()
            except OSError:
                continue
            found[path.relative_to(books_dir).as_posix()] = (stat.st_size, stat.st_mtime)
    return found


def guess_title(relative_path: str) -> str:
    return Path(relative_path).stem.replace("_", " ").strip() or relative_path


def scan(session: Session, books_dir: Path) -> ScanResult:
    """Register new and changed files (and queue them for parsing); mark vanished ones as missing."""
    found = find_books(books_dir)
    existing = {doc.path: doc for doc in session.scalars(select(DocumentRow))}
    result = ScanResult()

    for rel, (size, mtime) in found.items():
        doc = existing.get(rel)
        if doc is None:
            doc = DocumentRow(
                path=rel,
                format=FORMATS[Path(rel).suffix.lower()],
                size=size,
                mtime=mtime,
                title=guess_title(rel),
                status="pending",
            )
            session.add(doc)
            session.flush()
            result.added += 1
        elif doc.size != size or doc.mtime != mtime or doc.status == "missing":
            doc.size, doc.mtime, doc.status = size, mtime, "pending"
            result.changed += 1
        else:
            result.unchanged += 1
            continue
        jobs.enqueue(session, "parse", {"document_id": doc.id}, dedupe_key=f"parse:{doc.id}")

    for rel, doc in existing.items():
        if rel not in found and doc.status != "missing":
            doc.status = "missing"
            result.missing += 1

    session.commit()
    return result


def parse_document(session: Session, books_dir: Path, document_id: int) -> None:
    """Parse one file into sections (no LLM). A file that can't be parsed is marked 'error', not retried."""
    doc = session.get(DocumentRow, document_id)
    if doc is None or doc.status == "missing":
        return
    try:
        parsed = parse(books_dir / doc.path)
    except ParseError as exc:
        doc.status, doc.error = "error", str(exc)
        session.commit()
        log.warning("Can't parse %s: %s", doc.path, exc)
        return

    session.execute(delete(SectionRow).where(SectionRow.document_id == doc.id))
    for section in parsed.sections:
        session.add(
            SectionRow(
                document_id=doc.id,
                position=section.index,
                title=section.title,
                level=section.level,
                text=section.text,
                text_sha256=text_sha256(section.text),
                words=section.words,
                tokens=section.tokens,
            )
        )
    doc.sha256 = parsed.sha256
    doc.title = parsed.title
    doc.author = parsed.author
    doc.language = (parsed.language or "")[:35] or None
    doc.status, doc.error = "ready", None
    session.commit()
    log.info("Parsed %s: %d sections", doc.path, len(parsed.sections))


def to_models(doc: DocumentRow, section: SectionRow) -> tuple[Document, Section]:
    """Bridge database rows to the parser's dataclasses, which the summarizer works with."""
    model_section = Section(
        index=section.position, title=section.title, level=section.level, text=section.text
    )
    model_doc = Document(
        path=Path(doc.path),
        format=doc.format,
        title=doc.title,
        author=doc.author,
        language=doc.language,
        sha256=doc.sha256 or "",
        sections=(model_section,),
    )
    return model_doc, model_section
