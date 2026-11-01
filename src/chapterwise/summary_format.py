"""Structured summaries ("cards"): validating what the model returns, and simple text renderings.

The model is asked for JSON (see prompts.py). Anything it returns is untrusted: fields are type-checked,
trimmed and capped here, and every renderer escapes the text it outputs.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

MAX_FIELD = 600
MAX_IDEAS = 8
MAX_TERMS = 8
MAX_CHECKS = 4
READING_WPM = 200

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_BOLD = re.compile(r"\*\*(.+?)\*\*")


class InvalidSummary(ValueError):
    """The model's answer isn't a usable card."""


@dataclass(frozen=True)
class Idea:
    title: str
    point: str
    example: str
    emoji: str


@dataclass(frozen=True)
class Term:
    term: str
    meaning: str


@dataclass(frozen=True)
class Check:
    question: str
    answer: str


@dataclass(frozen=True)
class Card:
    tldr: str
    why: str
    ideas: tuple[Idea, ...]
    terms: tuple[Term, ...]
    takeaway: str
    action: str
    checks: tuple[Check, ...]

    def all_text(self) -> str:
        parts = [self.tldr, self.why, self.takeaway, self.action]
        parts += [f"{i.title} {i.point} {i.example}" for i in self.ideas]
        parts += [f"{t.term} {t.meaning}" for t in self.terms]
        parts += [f"{c.question} {c.answer}" for c in self.checks]
        return " ".join(p for p in parts if p)

    @property
    def minutes(self) -> int:
        """Reading time of the summary itself."""
        return max(1, math.ceil(len(strip_bold(self.all_text()).split()) / READING_WPM))


def strip_bold(text: str) -> str:
    return _BOLD.sub(r"\1", text)


def _text(value, limit: int = MAX_FIELD) -> str:
    if value is None:
        return ""
    if not isinstance(value, str | int | float):
        return ""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _emoji(value) -> str:
    text = _text(value, 16).replace(" ", "")
    # A short symbol only: no words, no markup.
    if not text or len(text) > 8 or any(ch.isalnum() or ch in "<>&\"'" for ch in text):
        return ""
    return text


def _items(value, limit: int) -> list[dict]:
    return [item for item in value if isinstance(item, dict)][:limit] if isinstance(value, list) else []


def parse_card(raw: str) -> Card:
    """Validate the model's JSON answer. Raises InvalidSummary if it can't be used."""
    try:
        data = json.loads(_FENCE.sub("", raw or ""))
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidSummary(f"not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidSummary("not a JSON object")

    ideas = tuple(
        Idea(
            title=_text(item.get("title"), 120),
            point=_text(item.get("point")),
            example=_text(item.get("example")),
            emoji=_emoji(item.get("emoji")),
        )
        for item in _items(data.get("key_ideas"), MAX_IDEAS)
    )
    ideas = tuple(i for i in ideas if i.title and i.point)
    tldr = _text(data.get("tldr"), 300)
    if not tldr or not ideas:
        raise InvalidSummary("missing tldr or key ideas")

    terms = tuple(
        Term(_text(item.get("term"), 80), _text(item.get("meaning"), 200))
        for item in _items(data.get("terms"), MAX_TERMS)
    )
    checks = tuple(
        Check(_text(item.get("question"), 300), _text(item.get("answer"), 400))
        for item in _items(data.get("quick_check"), MAX_CHECKS)
    )
    return Card(
        tldr=tldr,
        why=_text(data.get("why_it_matters")),
        ideas=ideas,
        terms=tuple(t for t in terms if t.term and t.meaning),
        takeaway=_text(data.get("takeaway")),
        action=_text(data.get("try_this")),
        checks=tuple(c for c in checks if c.question and c.answer),
    )


def try_parse_card(raw: str) -> Card | None:
    try:
        return parse_card(raw)
    except InvalidSummary:
        return None


def card_to_markdown(card: Card) -> str:
    """Plain Markdown version (CLI output, exports)."""
    lines = [f"**TL;DR:** {card.tldr}"]
    if card.why:
        lines += ["", f"**Why it matters:** {card.why}"]
    lines += ["", "### Key ideas"]
    for n, idea in enumerate(card.ideas, start=1):
        emoji = f"{idea.emoji} " if idea.emoji else ""
        lines.append(f"{n}. {emoji}**{strip_bold(idea.title)}**: {idea.point}")
        if idea.example:
            lines.append(f"   - *Example:* {idea.example}")
    if card.terms:
        lines += ["", "### Terms"]
        lines += [f"- **{t.term}**: {t.meaning}" for t in card.terms]
    if card.takeaway:
        lines += ["", f"**Takeaway:** {card.takeaway}"]
    if card.action:
        lines += ["", f"**Try this:** {card.action}"]
    if card.checks:
        lines += ["", "### Quick check"]
        lines += [f"- {c.question}\n  - *{c.answer}*" for c in card.checks]
    return "\n".join(lines)
