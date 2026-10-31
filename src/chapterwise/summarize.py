"""Section summaries: one call for sections that fit, map-reduce for longer ones. Everything is cached."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from chapterwise import prompts
from chapterwise.cache import Cache
from chapterwise.chunking import estimate_tokens, split_text
from chapterwise.llm import LLM
from chapterwise.models import Document, Section

# Rough allowance for the instructions wrapped around the text in each call.
PROMPT_OVERHEAD_TOKENS = 250


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


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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
        usage = Usage()
        key = self._key("section", section.text, doc, section)
        if (hit := self.cache.get(key)) is not None:
            usage.cache_hits += 1
            return hit["text"], usage

        chunks = split_text(section.text, self.max_chunk_tokens)
        if not chunks:
            raise ValueError(f"Section {section.index} ('{section.title}') has no text")

        if len(chunks) == 1:
            text = self._call(prompts.section_summary(doc.title, section.title, chunks[0]), usage)
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
            text = self._call(prompts.merge_notes(doc.title, section.title, "\n\n".join(notes)), usage)

        self.cache.set(key, {"text": text, "model": self.llm.model, "prompt_version": prompts.PROMPT_VERSION})
        return text, usage

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
        return {
            "kind": kind,
            "text_sha256": _sha(text),
            "book": doc.title,
            "section": section.title,
            "model": self.llm.model,
            "prompt_version": prompts.PROMPT_VERSION,
            **extra,
        }

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

    def _call(self, prompt: tuple[str, str], usage: Usage) -> str:
        completion = self.llm.complete(*prompt)
        usage.llm_calls += 1
        usage.tokens_in += completion.tokens_in
        usage.tokens_out += completion.tokens_out
        return completion.text
