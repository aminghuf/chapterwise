"""The parsed form of a book: metadata plus an ordered list of sections."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chapterwise.chunking import estimate_tokens


@dataclass(frozen=True)
class Section:
    index: int  # position in the document, 0-based
    title: str
    level: int  # 1 = top level of the table of contents
    text: str

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass(frozen=True)
class Document:
    path: Path
    format: str  # "epub" | "pdf" | "md"
    title: str
    author: str | None
    language: str | None
    sha256: str
    sections: tuple[Section, ...]
