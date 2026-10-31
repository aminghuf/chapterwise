"""Token estimates and splitting long text into pieces that fit the model."""

from __future__ import annotations

import math
import re

# Chinese, Japanese and Korean characters are roughly one token each; other text is ~4 chars per token.
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?\u3002\uff01\uff1f\u2026])\s+")


def estimate_tokens(text: str) -> int:
    """Approximate token count. Good enough for budgeting; not exact for any tokenizer."""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return math.ceil(cjk + (len(text) - cjk) / 4)


def split_text(text: str, max_tokens: int) -> list[str]:
    """Split text into chunks of at most ~max_tokens, preferring paragraph, then sentence boundaries."""
    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")
    if not text.strip():
        return []
    if estimate_tokens(text) <= max_tokens:
        return [text.strip()]

    pieces: list[str] = []
    for paragraph in _PARAGRAPH_BREAK.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if estimate_tokens(paragraph) <= max_tokens:
            pieces.append(paragraph)
        else:
            pieces.extend(_split_paragraph(paragraph, max_tokens))
    return _pack(pieces, max_tokens, "\n\n")


def _split_paragraph(paragraph: str, max_tokens: int) -> list[str]:
    sentences: list[str] = []
    for sentence in _SENTENCE_END.split(paragraph):
        if estimate_tokens(sentence) <= max_tokens:
            sentences.append(sentence)
        else:
            sentences.extend(_hard_split(sentence, max_tokens))
    return _pack(sentences, max_tokens, " ")


def _hard_split(text: str, max_tokens: int) -> list[str]:
    """Last resort for a single 'sentence' longer than the budget: cut by characters."""
    out: list[str] = []
    while text:
        cut = len(text)
        while cut > 1 and estimate_tokens(text[:cut]) > max_tokens:
            cut = max(1, int(cut * max_tokens / estimate_tokens(text[:cut])) - 1)
        out.append(text[:cut])
        text = text[cut:]
    return out


def _pack(pieces: list[str], max_tokens: int, separator: str) -> list[str]:
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}{separator}{piece}" if current else piece
        if current and estimate_tokens(candidate) > max_tokens:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
