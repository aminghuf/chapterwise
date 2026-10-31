"""Turn a book file into a Document with sections. No LLM involved."""

from __future__ import annotations

from pathlib import Path

from chapterwise.models import Document
from chapterwise.parsers._common import ParseError

SUPPORTED_EXTENSIONS = (".epub", ".pdf", ".md", ".markdown")

__all__ = ["SUPPORTED_EXTENSIONS", "ParseError", "parse"]


def parse(path: str | Path) -> Document:
    path = Path(path)
    if not path.is_file():
        raise ParseError(f"File not found: {path}")

    ext = path.suffix.lower()
    if ext == ".epub":
        from chapterwise.parsers.epub import parse_epub

        doc = parse_epub(path)
    elif ext == ".pdf":
        from chapterwise.parsers.pdf import parse_pdf

        doc = parse_pdf(path)
    elif ext in (".md", ".markdown"):
        from chapterwise.parsers.markdown import parse_markdown

        doc = parse_markdown(path)
    else:
        raise ParseError(f"Unsupported format '{ext}'. Supported: {', '.join(SUPPORTED_EXTENSIONS)}")

    if not doc.sections:
        raise ParseError(f"No readable text found in {path.name}")
    return doc
