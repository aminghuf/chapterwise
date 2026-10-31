"""EPUB: sections come from the book's own table of contents (NCX or EPUB3 nav).

The text of the reading order (spine) is extracted with a marker placed where each TOC entry points
(a whole file, or an anchor inside a file); splitting at those markers gives each chapter's text,
including chapters that share a file.
"""

from __future__ import annotations

import posixpath
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import ebooklib
from bs4 import BeautifulSoup, Comment, Declaration, Doctype, NavigableString, ProcessingInstruction
from ebooklib import epub

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

_BLOCK_TAGS = [
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "figcaption", "figure",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "ol", "p", "pre", "section",
    "table", "tr", "ul",
]  # fmt: skip
_HEADINGS = ["h1", "h2", "h3"]
_WHITESPACE = re.compile(r"\s+")
_MARKER = "\x00{}\x00"
_MARKER_RE = re.compile(r"\x00(\d+)\x00")


@dataclass
class _TocEntry:
    title: str
    level: int
    path: str
    fragment: str | None


def parse_epub(path: Path) -> Document:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            book = epub.read_epub(str(path))
    except Exception as exc:  # ebooklib raises a variety of errors for broken files
        raise ParseError(f"Can't read EPUB {path.name}: {exc}") from exc

    items = [
        item
        for idref, linear in book.spine
        if str(linear).lower() != "no"
        and (item := book.get_item_with_id(idref)) is not None
        and item.get_type() == ebooklib.ITEM_DOCUMENT
    ]
    if not items:
        raise ParseError(f"{path.name} has no readable chapters")

    entries = _flatten_toc(book.toc)
    raw = _sections_from_toc(items, entries) if entries else []
    if not raw:
        raw = [_section_from_item(item) for item in items]

    return Document(
        path=path,
        format="epub",
        title=_metadata(book, "title") or path.stem,
        author=_metadata(book, "creator"),
        language=_metadata(book, "language"),
        sha256=file_sha256(path),
        sections=finalize(raw),
    )


def _flatten_toc(nodes, level: int = 1) -> list[_TocEntry]:
    out: list[_TocEntry] = []
    for node in nodes:
        if isinstance(node, tuple | list):  # (Section, children)
            head, children = node[0], node[1]
            if getattr(head, "href", None):
                out.append(_entry(head, level))
            out.extend(_flatten_toc(children, level + 1))
        elif getattr(node, "href", None):
            out.append(_entry(node, level))
    return out


def _entry(node, level: int) -> _TocEntry:
    target, _, fragment = unquote(node.href).partition("#")
    return _TocEntry(
        title=_WHITESPACE.sub(" ", node.title or "").strip(),
        level=level,
        path=posixpath.normpath(target) if target else "",
        fragment=fragment or None,
    )


def _sections_from_toc(items, entries: list[_TocEntry]) -> list[RawSection]:
    target_level = choose_level([e.level for e in entries])
    names = [item.get_name() for item in items]

    per_item: dict[int, list[int]] = {}
    for n, entry in enumerate(entries):
        if entry.level > target_level:
            continue
        item_index = _match_item(entry.path, names)
        if item_index is not None:
            per_item.setdefault(item_index, []).append(n)

    if not per_item:
        return []

    texts = [_item_text(item, entries, per_item.get(i, [])) for i, item in enumerate(items)]
    parts = _MARKER_RE.split("\n\n".join(texts))

    raw: list[RawSection] = []
    front = parts[0]
    if word_count(front) >= MIN_FRONT_MATTER_WORDS:
        raw.append(RawSection("Opening pages", target_level, front))
    for i in range(1, len(parts), 2):
        entry = entries[int(parts[i])]
        raw.append(RawSection(entry.title, entry.level, parts[i + 1]))
    return raw


def _match_item(target: str, names: list[str]) -> int | None:
    """Find the spine item a TOC href points to. Hrefs may be relative to different folders."""
    if not target:
        return None
    normalized = [posixpath.normpath(name) for name in names]
    if target in normalized:
        return normalized.index(target)
    suffix_matches = [
        i for i, name in enumerate(normalized) if name.endswith("/" + target) or target.endswith("/" + name)
    ]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    base = posixpath.basename(target)
    base_matches = [i for i, name in enumerate(normalized) if posixpath.basename(name) == base]
    return base_matches[0] if len(base_matches) == 1 else None


def _soup(item) -> BeautifulSoup:
    soup = BeautifulSoup(item.get_content(), "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    for node in list(soup.find_all(string=True)):
        if isinstance(node, Comment | Declaration | Doctype | ProcessingInstruction):
            node.extract()
            continue
        # HTML treats source line breaks and indentation as plain spaces.
        collapsed = _WHITESPACE.sub(" ", str(node))
        if collapsed != str(node):
            node.replace_with(collapsed)
    return soup


def _item_text(item, entries: list[_TocEntry], entry_ids: list[int]) -> str:
    soup = _soup(item)
    body = soup.body or soup

    at_start: list[str] = []
    for n in entry_ids:
        fragment = entries[n].fragment
        anchor = (soup.find(id=fragment) or soup.find(attrs={"name": fragment})) if fragment else None
        if anchor is not None:
            anchor.insert_before(NavigableString(_MARKER.format(n)))
        else:
            at_start.append(_MARKER.format(n))
    if at_start:
        body.insert(0, NavigableString("".join(at_start)))

    return _to_text(soup)


def _to_text(soup: BeautifulSoup) -> str:
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert(0, "\n\n")
        tag.append("\n\n")
    return soup.get_text()


def _section_from_item(item) -> RawSection:
    soup = _soup(item)
    heading = soup.find(_HEADINGS)
    title = heading.get_text(" ", strip=True) if heading else Path(item.get_name()).stem
    return RawSection(title, 1, _to_text(soup))


def _metadata(book, name: str) -> str | None:
    values = book.get_metadata("DC", name)
    if values and values[0] and values[0][0]:
        return _WHITESPACE.sub(" ", str(values[0][0])).strip() or None
    return None
