"""Helpers shared by all parsers."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from chapterwise.models import Section

# Sections shorter than this are title pages, part dividers or empty anchors: dropped.
MIN_SECTION_WORDS = 20
# Text before the first chapter of an EPUB/PDF is kept only if it is this long (a real preface,
# not a cover and copyright page).
MIN_FRONT_MATTER_WORDS = 300

_INLINE_SPACE = re.compile(r"[ \t\f\v\u00a0]+")
_MANY_BLANK_LINES = re.compile(r"\n{3,}")


class ParseError(Exception):
    """The file can't be turned into sections."""


@dataclass
class RawSection:
    title: str
    level: int
    text: str


def normalize_text(text: str, keep_indent: bool = False) -> str:
    """Tidy whitespace: one space between words, at most one blank line between paragraphs.

    keep_indent=True only trims line ends (for Markdown, where indentation can be meaningful).
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if keep_indent:
        lines = [line.rstrip() for line in lines]
    else:
        lines = [_INLINE_SPACE.sub(" ", line).strip() for line in lines]
    return _MANY_BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def word_count(text: str) -> int:
    return len(text.split())


def choose_level(levels: Sequence[int], min_sections: int = 3) -> int:
    """Pick the table-of-contents depth to split at.

    Use the top level, unless it has too few entries (e.g. a book with only "Part I" and "Part II"),
    in which case go one level deeper, and so on.
    """
    if not levels:
        return 1
    target, deepest = min(levels), max(levels)
    while target < deepest and sum(1 for level in levels if level <= target) < min_sections:
        target += 1
    return target


def finalize(raw: Sequence[RawSection], keep_indent: bool = False) -> tuple[Section, ...]:
    cleaned = [(r.title.strip() or "Untitled", r.level, normalize_text(r.text, keep_indent)) for r in raw]
    kept = [c for c in cleaned if word_count(c[2]) >= MIN_SECTION_WORDS]
    if not kept:  # a very short document: keep whatever has text rather than nothing
        kept = [c for c in cleaned if c[2]]
    return tuple(
        Section(index=i, title=title, level=level, text=text) for i, (title, level, text) in enumerate(kept)
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
