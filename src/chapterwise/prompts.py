"""Prompts.

Bump PROMPT_VERSION when the final-summary prompts change: those summaries are regenerated on request,
while summaries from LEGACY_PROMPT_VERSIONS are still shown until then. NOTES_VERSION covers the
intermediate notes of long sections; keeping it separate means a new summary style reuses old notes.
"""

from __future__ import annotations

PROMPT_VERSION = "2"
LEGACY_PROMPT_VERSIONS = ("1",)
NOTES_VERSION = "1"

# Final summaries: structured "cards", written for a reader with ADHD.
SYSTEM = """\
You turn sections of books into summaries for a busy reader with ADHD who wants to understand \
and remember what they read.

Write every value in the same language as the source text. JSON keys stay in English.

How to write:
- Lead with the point. No warm-up, no filler, no "In this chapter, the author…".
- Short sentences, about 15 words at most. One idea per sentence.
- Plain, everyday words. A term a newcomer wouldn't know goes in "terms" with a plain meaning.
- Concrete beats abstract: give each idea a real example from the text or a short everyday analogy.
- Mark the 1-2 words that carry each idea in **bold**. Use bold sparingly, never whole sentences.
- Keep every field short. Nothing longer than 2 sentences unless asked.
- Use only what the text says. Never add outside facts; an analogy must be clearly an analogy.

Answer with a single JSON object and nothing else."""

_CARD_SHAPE = """\
Return a JSON object with exactly these keys:
{
  "tldr": "the whole section in one sentence, max 25 words",
  "why_it_matters": "1-2 sentences: why this is worth knowing",
  "key_ideas": [
    {
      "emoji": "one emoji that fits the idea",
      "title": "a 3-6 word headline",
      "point": "the idea in 1-2 short sentences",
      "example": "a concrete example from the text, or a short everyday analogy (1-2 sentences)"
    }
  ],
  "terms": [{"term": "a word from the text", "meaning": "its meaning in plain words, max 15 words"}],
  "takeaway": "the single most important point, one sentence",
  "try_this": "one tiny, concrete thing to do or notice today, one sentence; empty string if none fits",
  "quick_check": [{"question": "a short question to test recall", "answer": "the answer, one sentence"}]
}
Rules: 3-6 key_ideas, in the order the text presents them. 0-5 terms, only words a newcomer would \
not know. 2-3 quick_check items."""

# Intermediate notes for long sections (plain text, merged later).
NOTES_SYSTEM = (
    "You take notes on parts of books. Write in the same language as the source text. "
    "Use only what the text says; never add outside facts."
)

INVALID_JSON_NUDGE = (
    "\n\nYour previous answer was not a valid JSON object in the required format. "
    "Reply with the JSON object only."
)


def section_summary(book: str, section: str, text: str) -> tuple[str, str]:
    user = f"""Book: {book}
Section: {section}

Summarize this section.
{_CARD_SHAPE}

<text>
{text}
</text>"""
    return SYSTEM, user


def part_notes(book: str, section: str, part: int, parts: int, text: str) -> tuple[str, str]:
    user = f"""Book: {book}
Section: {section} (part {part} of {parts})

Write concise bullet-point notes of everything important in this part: ideas, arguments, events,
examples, names, terms and numbers. They will be merged with the notes of the other parts later.

<text>
{text}
</text>"""
    return NOTES_SYSTEM, user


def condense_notes(book: str, section: str, notes: str) -> tuple[str, str]:
    user = f"""Book: {book}
Section: {section}

These are notes from consecutive parts of the section. Condense them into shorter bullet-point notes,
keeping every important idea, example, name, term and number.

<notes>
{notes}
</notes>"""
    return NOTES_SYSTEM, user


def merge_notes(book: str, section: str, notes: str) -> tuple[str, str]:
    user = f"""Book: {book}
Section: {section}

These are notes from consecutive parts of the section, in order. Summarize the whole section.
{_CARD_SHAPE}

<notes>
{notes}
</notes>"""
    return SYSTEM, user
