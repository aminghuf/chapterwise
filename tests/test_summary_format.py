import json

import pytest

from chapterwise.summary_format import InvalidSummary, card_to_markdown, parse_card, try_parse_card
from tests.conftest import card_json


def test_parses_a_valid_card():
    card = parse_card(card_json(tldr="Focus is a budget."))
    assert card.tldr == "Focus is a budget."
    assert [i.title for i in card.ideas] == ["Attention is limited", "Short sprints work"]
    assert card.ideas[0].emoji == "🧠" and card.ideas[0].example.startswith("Like")
    assert card.terms[0].term == "Pomodoro"
    assert card.checks[0].answer == "25 minutes."
    assert card.minutes == 1


def test_tolerates_code_fences_and_missing_optional_fields():
    raw = "```json\n" + json.dumps({"tldr": "x", "key_ideas": [{"title": "T", "point": "P"}]}) + "\n```"
    card = parse_card(raw)
    assert card.ideas[0].example == "" and card.terms == () and card.checks == () and card.action == ""


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        json.dumps({"tldr": "", "key_ideas": [{"title": "T", "point": "P"}]}),
        json.dumps({"tldr": "x", "key_ideas": []}),
        json.dumps({"tldr": "x", "key_ideas": [{"title": "", "point": "P"}]}),
    ],
)
def test_rejects_unusable_answers(raw):
    with pytest.raises(InvalidSummary):
        parse_card(raw)
    assert try_parse_card(raw) is None


def test_cleans_untrusted_fields():
    raw = card_json(
        key_ideas=[
            {"emoji": "<b>", "title": "  spaced\n  out  ", "point": "p" * 5000, "example": 42},
            {"emoji": "not an emoji", "title": "T", "point": "P", "example": ["list"]},
        ]
        + [{"title": f"T{n}", "point": "P"} for n in range(20)],
        terms="not a list",
    )
    card = parse_card(raw)
    first, second = card.ideas[0], card.ideas[1]
    assert first.emoji == "" and second.emoji == ""
    assert first.title == "spaced out"
    assert len(first.point) <= 600 and first.point.endswith("…")
    assert first.example == "42" and second.example == ""
    assert len(card.ideas) == 8 and card.terms == ()


def test_markdown_rendering():
    md = card_to_markdown(parse_card(card_json(tldr="One line.")))
    assert md.startswith("**TL;DR:** One line.")
    assert "1. 🧠 **Attention is limited**" in md
    assert "### Quick check" in md and "*25 minutes.*" in md
