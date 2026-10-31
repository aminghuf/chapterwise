"""Prompts. Bump PROMPT_VERSION whenever a prompt changes, so cached summaries are regenerated."""

from __future__ import annotations

PROMPT_VERSION = "1"

SYSTEM = (
    "You summarize sections of books for a reader who wants to understand and remember them. "
    "Always write in the same language as the source text. "
    "Use only what the text says; never add outside facts. Answer in Markdown without a top-level title."
)

_SUMMARY_SHAPE = """\
- Start with a 1-2 sentence overview.
- Then the key ideas as bullet points (at most 8), each with a short explanation.
- Keep the names, terms and numbers the text uses.
- End with one line starting with "**Takeaway:**" giving the single most important point."""


def section_summary(book: str, section: str, text: str) -> tuple[str, str]:
    user = f"""Book: {book}
Section: {section}

Summarize this section.
{_SUMMARY_SHAPE}

<text>
{text}
</text>"""
    return SYSTEM, user


def part_notes(book: str, section: str, part: int, parts: int, text: str) -> tuple[str, str]:
    user = f"""Book: {book}
Section: {section} (part {part} of {parts})

Write concise bullet-point notes of everything important in this part: ideas, arguments, events,
names, terms and numbers. They will be merged with the notes of the other parts later.

<text>
{text}
</text>"""
    return SYSTEM, user


def condense_notes(book: str, section: str, notes: str) -> tuple[str, str]:
    user = f"""Book: {book}
Section: {section}

These are notes from consecutive parts of the section. Condense them into shorter bullet-point notes,
keeping every important idea, name, term and number.

<notes>
{notes}
</notes>"""
    return SYSTEM, user


def merge_notes(book: str, section: str, notes: str) -> tuple[str, str]:
    user = f"""Book: {book}
Section: {section}

These are notes from consecutive parts of the section, in order. Write the summary of the whole section.
{_SUMMARY_SHAPE}

<notes>
{notes}
</notes>"""
    return SYSTEM, user
