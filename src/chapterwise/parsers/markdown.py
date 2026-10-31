"""Markdown: sections come from headings. A single top '# Title' is treated as the document title."""

from __future__ import annotations

import re
from pathlib import Path

from chapterwise.models import Document
from chapterwise.parsers._common import RawSection, choose_level, file_sha256, finalize

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_MARKUP = re.compile(r"[*_`]")


def parse_markdown(path: Path) -> Document:
    meta, body = _split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    lines = body.split("\n")
    headings = _find_headings(lines)

    title = meta.get("title")
    content_start = 0
    if headings and headings[0][1] == 1 and sum(1 for h in headings if h[1] == 1) == 1:
        title = title or headings[0][2]
        content_start = headings[0][0] + 1
        headings = headings[1:]
    title = title or path.stem.replace("-", " ").replace("_", " ")

    # Notes and playbooks are shorter than books: two top-level headings are already a useful split.
    target_level = choose_level([level for _, level, _ in headings], min_sections=2)
    bounds = [h for h in headings if h[1] <= target_level]

    raw: list[RawSection] = []
    first_line = bounds[0][0] if bounds else len(lines)
    intro = "\n".join(lines[content_start:first_line])
    if intro.strip():
        raw.append(RawSection("Introduction" if bounds else title, target_level, intro))
    for i, (line, level, heading) in enumerate(bounds):
        end = bounds[i + 1][0] if i + 1 < len(bounds) else len(lines)
        raw.append(RawSection(heading, level, "\n".join(lines[line + 1 : end])))

    return Document(
        path=path,
        format="md",
        title=title,
        author=meta.get("author"),
        language=meta.get("language") or meta.get("lang"),
        sha256=file_sha256(path),
        sections=finalize(raw, keep_indent=True),
    )


def _find_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, title) for every ATX heading outside fenced code blocks."""
    headings: list[tuple[int, int, str]] = []
    fence: str | None = None
    for i, line in enumerate(lines):
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is None and (m := _HEADING.match(line)):
            headings.append((i, len(m.group(1)), _INLINE_MARKUP.sub("", m.group(2)).strip()))
    return headings


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Minimal YAML front matter: only simple `key: value` lines are read."""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[4:end].split("\n"):
        key, sep, value = line.partition(":")
        if sep and key.strip() and value.strip():
            meta[key.strip().lower()] = value.strip().strip("'\"")
    body_start = text.find("\n", end + 4)
    return meta, text[body_start + 1 :] if body_start != -1 else ""
