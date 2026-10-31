"""PDF: sections come from the PDF outline (bookmarks); without one, fixed page ranges."""

from __future__ import annotations

import logging
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from chapterwise.models import Document
from chapterwise.parsers._common import (
    MIN_FRONT_MATTER_WORDS,
    ParseError,
    RawSection,
    choose_level,
    file_sha256,
    finalize,
    word_count,
)

PAGES_PER_SECTION = 10  # used only when the PDF has no outline

logging.getLogger("pypdf").setLevel(logging.ERROR)


def parse_pdf(path: Path) -> Document:
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ParseError(f"{path.name} is password-protected")
        pages = [_page_text(page) for page in reader.pages]
        outline = _flatten_outline(reader, reader.outline)
    except PdfReadError as exc:
        raise ParseError(f"Can't read PDF {path.name}: {exc}") from exc

    if not any(word_count(text) for text in pages):
        raise ParseError(f"{path.name} has no extractable text; it looks scanned (OCR isn't supported yet)")

    raw = _sections_from_outline(pages, outline) if outline else []
    if not raw:
        raw = _fixed_ranges(pages)

    meta = reader.metadata
    return Document(
        path=path,
        format="pdf",
        title=((meta.title if meta else None) or path.stem).strip(),
        author=((meta.author if meta else None) or "").strip() or None,
        language=None,
        sha256=file_sha256(path),
        sections=finalize(raw),
    )


def _page_text(page) -> str:
    try:
        return page.extract_text() or ""
    except Exception:  # one damaged page shouldn't fail the whole book
        return ""


def _flatten_outline(reader: PdfReader, nodes, level: int = 1) -> list[tuple[str, int, int]]:
    """Outline as (title, level, page index). In pypdf a nested list holds the previous item's children."""
    out: list[tuple[str, int, int]] = []
    for node in nodes:
        if isinstance(node, list):
            out.extend(_flatten_outline(reader, node, level + 1))
            continue
        try:
            page = reader.get_destination_page_number(node)
        except Exception:
            continue
        if page is not None and page >= 0:
            out.append((str(node.title or "").strip(), level, page))
    return out


def _sections_from_outline(pages: list[str], outline: list[tuple[str, int, int]]) -> list[RawSection]:
    target_level = choose_level([level for _, level, _ in outline])
    starts = sorted(
        ((title, level, page) for title, level, page in outline if level <= target_level),
        key=lambda entry: entry[2],
    )
    raw: list[RawSection] = []

    front = "\n\n".join(pages[: starts[0][2]])
    if word_count(front) >= MIN_FRONT_MATTER_WORDS:
        raw.append(RawSection("Opening pages", target_level, front))

    for i, (title, level, start) in enumerate(starts):
        end = starts[i + 1][2] if i + 1 < len(starts) else len(pages)
        end = max(end, start + 1)  # two entries starting on the same page share that page
        raw.append(RawSection(title, level, "\n\n".join(pages[start:end])))
    return raw


def _fixed_ranges(pages: list[str]) -> list[RawSection]:
    raw = []
    for start in range(0, len(pages), PAGES_PER_SECTION):
        end = min(start + PAGES_PER_SECTION, len(pages))
        raw.append(RawSection(f"Pages {start + 1}-{end}", 1, "\n\n".join(pages[start:end])))
    return raw
