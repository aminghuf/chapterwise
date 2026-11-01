"""Section summaries: one call for sections that fit, map-reduce for longer ones. Everything is cached.

A finished summary is stored as {"text": ..., "format": "card" | "markdown"}: "card" is the validated
JSON of summary_format.Card; "markdown" is the fallback when the model doesn't return usable JSON, and
the format of summaries made with older prompt versions.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from chapterwise import prompts
from chapterwise.cache import Cache
from chapterwise.chunking import estimate_tokens, split_text
from chapterwise.llm import LLM
from chapterwise.models import Document, Section
from chapterwise.summary_format import InvalidSummary, parse_card

log = logging.getLogger(__name__)

# Rough allowance for the instructions wrapped around the text in each call.
PROMPT_OVERHEAD_TOKENS = 600


@dataclass
class Usage:
    llm_calls: int = 0
    cache_hits: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    def __iadd__(self, other: Usage) -> Usage:
        self.llm_calls += other.llm_calls
        self.cache_hits += other.cache_hits
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        return self


@dataclass(frozen=True)
class Estimate:
    parts: int
    llm_calls: int
    input_tokens: int


@dataclass(frozen=True)
class StoredSummary:
    text: str
    format: str  # "card" | "markdown"
    current: bool  # made with the current prompt version


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def cache_key(
    kind: str,
    text_hash: str,
    book_title: str,
    section_title: str,
    model: str,
    prompt_version: str | None = None,
    **extra: str,
) -> dict[str, str]:
    """The cache key of one LLM result. kind="section" is a finished section summary."""
    if prompt_version is None:
        prompt_version = prompts.PROMPT_VERSION if kind == "section" else prompts.NOTES_VERSION
    return {
        "kind": kind,
        "text_sha256": text_hash,
        "book": book_title,
        "section": section_title,
        "model": model,
        "prompt_version": prompt_version,
        **extra,
    }


def summary_keys(text_hash: str, book_title: str, section_title: str, model: str) -> list[dict[str, str]]:
    """Keys a finished summary may be stored under: current prompt version first, then older ones."""
    versions = (prompts.PROMPT_VERSION, *prompts.LEGACY_PROMPT_VERSIONS)
    return [cache_key("section", text_hash, book_title, section_title, model, v) for v in versions]


def find_summary(
    cache: Cache, text_hash: str, book_title: str, section_title: str, model: str
) -> StoredSummary | None:
    """The newest stored summary of a section, if any."""
    for n, key in enumerate(summary_keys(text_hash, book_title, section_title, model)):
        if (entry := cache.get(key)) is not None:
            return StoredSummary(entry["text"], entry.get("format", "markdown"), current=n == 0)
    return None


class Summarizer:
    def __init__(self, llm: LLM, cache: Cache, max_chunk_tokens: int = 24_000):
        self.llm = llm
        self.cache = cache
        self.max_chunk_tokens = max_chunk_tokens

    def estimate(self, section: Section) -> Estimate:
        """What a fresh (uncached) summary would cost, approximately, without calling the API."""
        chunks = split_text(section.text, self.max_chunk_tokens)
        text_tokens = sum(estimate_tokens(c) for c in chunks)
        if len(chunks) <= 1:
            return Estimate(
                parts=len(chunks), llm_calls=len(chunks), input_tokens=text_tokens + PROMPT_OVERHEAD_TOKENS
            )
        calls = len(chunks) + 1
        notes_tokens = len(chunks) * 800  # typical size of one part's notes
        return Estimate(
            parts=len(chunks),
            llm_calls=calls,
            input_tokens=text_tokens + notes_tokens + calls * PROMPT_OVERHEAD_TOKENS,
        )

    def is_cached(self, doc: Document, section: Section) -> bool:
        return self.cache.get(self._key("section", section.text, doc, section)) is not None

    def summarize_section(self, doc: Document, section: Section) -> tuple[str, Usage]:
        """The section's summary text (card JSON, or Markdown as a fallback) and what it cost."""
        usage = Usage()
        key = self._key("section", section.text, doc, section)
        if (hit := self.cache.get(key)) is not None:
            usage.cache_hits += 1
            return hit["text"], usage

        chunks = split_text(section.text, self.max_chunk_tokens)
        if not chunks:
            raise ValueError(f"Section {section.index} ('{section.title}') has no text")

        if len(chunks) == 1:
            prompt = prompts.section_summary(doc.title, section.title, chunks[0])
        else:
            notes = [
                self._cached_call(
                    "part_notes",
                    chunk,
                    doc,
                    section,
                    prompts.part_notes(doc.title, section.title, n, len(chunks), chunk),
                    usage,
                    part=f"{n}/{len(chunks)}",
                )
                for n, chunk in enumerate(chunks, start=1)
            ]
            notes = self._condense(doc, section, notes, usage)
            prompt = prompts.merge_notes(doc.title, section.title, "\n\n".join(notes))

        text, fmt = self._card(prompt, usage)
        self.cache.set(
            key,
            {"text": text, "format": fmt, "model": self.llm.model, "prompt_version": prompts.PROMPT_VERSION},
        )
        return text, usage

    def _card(self, prompt: tuple[str, str], usage: Usage) -> tuple[str, str]:
        """Ask for the JSON card; retry once if it's unusable, then keep the answer as Markdown."""
        system, user = prompt
        answer = self._call((system, user), usage, json_mode=True)
        for attempt in (1, 2):
            try:
                parse_card(answer)
                return answer, "card"
            except InvalidSummary as exc:
                if attempt == 2:
                    log.warning("Model answer isn't a valid card (%s); keeping it as Markdown", exc)
                    return answer, "markdown"
                answer = self._call((system, user + prompts.INVALID_JSON_NUDGE), usage, json_mode=True)
        raise AssertionError("unreachable")

    def _condense(self, doc: Document, section: Section, notes: list[str], usage: Usage) -> list[str]:
        """Shrink the notes until they fit in one merge call (only needed for very long sections)."""
        while len(notes) > 1 and estimate_tokens("\n\n".join(notes)) > self.max_chunk_tokens:
            batches = split_text("\n\n".join(notes), self.max_chunk_tokens)
            if len(batches) >= len(notes):  # can't shrink further by batching
                break
            notes = [
                self._cached_call(
                    "condense",
                    batch,
                    doc,
                    section,
                    prompts.condense_notes(doc.title, section.title, batch),
                    usage,
                )
                for batch in batches
            ]
        return notes

    def _key(self, kind: str, text: str, doc: Document, section: Section, **extra: str) -> dict[str, str]:
        return cache_key(kind, text_sha256(text), doc.title, section.title, self.llm.model, **extra)

    def _cached_call(
        self,
        kind: str,
        text: str,
        doc: Document,
        section: Section,
        prompt: tuple[str, str],
        usage: Usage,
        **extra: str,
    ) -> str:
        key = self._key(kind, text, doc, section, **extra)
        if (hit := self.cache.get(key)) is not None:
            usage.cache_hits += 1
            return hit["text"]
        result = self._call(prompt, usage)
        self.cache.set(key, {"text": result})
        return result

    def _call(self, prompt: tuple[str, str], usage: Usage, json_mode: bool = False) -> str:
        completion = self.llm.complete(*prompt, json_mode=json_mode)
        usage.llm_calls += 1
        usage.tokens_in += completion.tokens_in
        usage.tokens_out += completion.tokens_out
        return completion.text
