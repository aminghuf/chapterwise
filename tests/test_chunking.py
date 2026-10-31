import re

import pytest

from chapterwise.chunking import estimate_tokens, split_text


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd" * 100) == 100
    assert estimate_tokens("书" * 10) == 10  # CJK counts ~1 token per character


def test_short_text_is_one_chunk():
    assert split_text("Hello world.", 100) == ["Hello world."]
    assert split_text("   \n\n ", 100) == []


def test_chunks_respect_budget_and_keep_all_text():
    paragraphs = [f"Paragraph {i}. " + "word " * 40 for i in range(30)]
    text = "\n\n".join(paragraphs)
    chunks = split_text(text, 200)
    assert len(chunks) > 1
    assert all(estimate_tokens(c) <= 200 for c in chunks)
    assert squash("".join(chunks)) == squash(text)


def test_long_paragraph_splits_on_sentences_then_characters():
    long_paragraph = " ".join(f"Sentence number {i} is here." for i in range(200))
    unbroken = "x" * 5000
    text = long_paragraph + "\n\n" + unbroken
    chunks = split_text(text, 100)
    assert all(estimate_tokens(c) <= 100 for c in chunks)
    assert squash("".join(chunks)) == squash(text)
    assert any(c.endswith("is here.") for c in chunks)  # sentence boundaries are preferred


def test_rejects_bad_budget():
    with pytest.raises(ValueError):
        split_text("text", 0)
