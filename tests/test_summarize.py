from pathlib import Path

from chapterwise import prompts
from chapterwise.cache import FileCache
from chapterwise.llm import Completion
from chapterwise.models import Document, Section
from chapterwise.summarize import Summarizer, cache_key, find_summary, text_sha256
from chapterwise.summary_format import parse_card
from tests.conftest import FakeLLM, card_json


def make_doc(text: str) -> tuple[Document, Section]:
    section = Section(index=0, title="Chapter 1", level=1, text=text)
    doc = Document(Path("b.epub"), "epub", "Book", None, None, "0" * 64, (section,))
    return doc, section


def long_text(paragraphs: int = 12) -> str:
    return "\n\n".join(f"Paragraph {i}. " + "word " * 80 for i in range(paragraphs))


def test_short_section_is_one_call(tmp_path, fake_llm):
    doc, section = make_doc("A short chapter about caching.")
    summary, usage = Summarizer(fake_llm, FileCache(tmp_path)).summarize_section(doc, section)

    assert parse_card(summary).tldr == "summary #1"
    assert fake_llm.json_modes == [True]  # the final summary is requested as JSON
    assert usage.llm_calls == 1 and usage.cache_hits == 0
    assert "A short chapter about caching." in fake_llm.calls[0]
    assert "Section: Chapter 1" in fake_llm.calls[0]


def test_long_section_is_map_reduced(tmp_path, fake_llm):
    doc, section = make_doc(long_text())
    summarizer = Summarizer(fake_llm, FileCache(tmp_path), max_chunk_tokens=300)
    summary, usage = summarizer.summarize_section(doc, section)

    parts = [c for c in fake_llm.calls if "(part " in c]
    assert len(parts) >= 2
    assert usage.llm_calls == len(parts) + 1
    assert parse_card(summary).tldr == f"merged summary #{len(fake_llm.calls)}"
    assert fake_llm.json_modes == [False] * len(parts) + [True]  # notes are plain text
    # The merge call receives every part's notes.
    merge_prompt = fake_llm.calls[-1]
    for n in range(1, len(parts) + 1):
        assert f"notes #{n}" in merge_prompt
    # Together the parts cover the whole section.
    for i in range(12):
        assert any(f"Paragraph {i}." in p for p in parts)


def test_second_run_is_served_from_cache(tmp_path):
    doc, section = make_doc(long_text())
    first = FakeLLM()
    summary1, _ = Summarizer(first, FileCache(tmp_path), max_chunk_tokens=300).summarize_section(doc, section)

    second = FakeLLM()
    summary2, usage = Summarizer(second, FileCache(tmp_path), max_chunk_tokens=300).summarize_section(
        doc, section
    )
    assert second.calls == []
    assert usage.cache_hits == 1 and usage.llm_calls == 0
    assert summary2 == summary1


def test_interrupted_run_reuses_finished_parts(tmp_path):
    doc, section = make_doc(long_text())
    cache = FileCache(tmp_path)
    Summarizer(FakeLLM(), cache, max_chunk_tokens=300).summarize_section(doc, section)
    # Drop only the final summary, as if the run died before the merge finished.
    final_key = Summarizer(FakeLLM(), cache)._key("section", section.text, doc, section)
    cache._path(final_key).unlink()

    resumed = FakeLLM()
    _, usage = Summarizer(resumed, cache, max_chunk_tokens=300).summarize_section(doc, section)
    assert usage.llm_calls == 1  # only the merge is redone
    assert usage.cache_hits >= 2


def test_model_or_prompt_change_invalidates_cache(tmp_path, monkeypatch):
    doc, section = make_doc("Some chapter text that is long enough.")
    cache = FileCache(tmp_path)
    Summarizer(FakeLLM("model-a"), cache).summarize_section(doc, section)

    other_model = FakeLLM("model-b")
    Summarizer(other_model, cache).summarize_section(doc, section)
    assert len(other_model.calls) == 1

    monkeypatch.setattr(prompts, "PROMPT_VERSION", "999")
    new_prompt = FakeLLM("model-a")
    Summarizer(new_prompt, cache).summarize_section(doc, section)
    assert len(new_prompt.calls) == 1


def test_very_long_section_condenses_notes(tmp_path, fake_llm):
    doc, section = make_doc(long_text(60))
    # Tiny budget: the notes of all parts don't fit in one merge call, so they get condensed first.
    summarizer = Summarizer(fake_llm, FileCache(tmp_path), max_chunk_tokens=120)
    summary, _ = summarizer.summarize_section(doc, section)
    assert parse_card(summary).tldr.startswith("merged summary")


def test_estimate(fake_llm, tmp_path):
    _doc, section = make_doc(long_text())
    est = Summarizer(fake_llm, FileCache(tmp_path), max_chunk_tokens=300).estimate(section)
    assert est.parts >= 2
    assert est.llm_calls == est.parts + 1
    assert est.input_tokens > section.tokens
    assert fake_llm.calls == []


class ScriptedLLM(FakeLLM):
    """Returns the given answers in order."""

    def __init__(self, *answers: str):
        super().__init__()
        self.answers = list(answers)

    def complete(self, system, user, json_mode=False):
        self.calls.append(user)
        self.json_modes.append(json_mode)
        return Completion(self.answers.pop(0), 10, 10)


def test_invalid_json_is_retried_once(tmp_path):
    doc, section = make_doc("Some chapter text.")
    llm = ScriptedLLM("Sure! Here is your summary.", card_json(tldr="second try"))
    cache = FileCache(tmp_path)
    summary, usage = Summarizer(llm, cache).summarize_section(doc, section)
    assert parse_card(summary).tldr == "second try"
    assert usage.llm_calls == 2
    assert "not a valid JSON object" in llm.calls[1]
    stored = find_summary(cache, text_sha256(section.text), "Book", "Chapter 1", "fake-model")
    assert (stored.format, stored.current) == ("card", True)


def test_unusable_answer_is_kept_as_markdown(tmp_path):
    doc, section = make_doc("Some chapter text.")
    llm = ScriptedLLM("**Plain** summary", '{"tldr": ""}')
    cache = FileCache(tmp_path)
    summary, _ = Summarizer(llm, cache).summarize_section(doc, section)
    stored = find_summary(cache, text_sha256(section.text), "Book", "Chapter 1", "fake-model")
    assert stored.format == "markdown" and stored.text == summary == '{"tldr": ""}'


def test_old_summaries_are_still_found(tmp_path):
    cache = FileCache(tmp_path)
    sha = text_sha256("text")
    cache.set(cache_key("section", sha, "Book", "Ch", "m", prompt_version="1"), {"text": "old **md**"})
    stored = find_summary(cache, sha, "Book", "Ch", "m")
    assert (stored.text, stored.format, stored.current) == ("old **md**", "markdown", False)
    cache.set(cache_key("section", sha, "Book", "Ch", "m"), {"text": card_json(), "format": "card"})
    assert find_summary(cache, sha, "Book", "Ch", "m").current


def test_new_summary_style_reuses_notes(tmp_path, monkeypatch):
    """Bumping the summary prompt regenerates only the final call, not the notes of long sections."""
    doc, section = make_doc(long_text())
    cache = FileCache(tmp_path)
    Summarizer(FakeLLM(), cache, max_chunk_tokens=300).summarize_section(doc, section)
    monkeypatch.setattr(prompts, "PROMPT_VERSION", "999")
    again = FakeLLM()
    _, usage = Summarizer(again, cache, max_chunk_tokens=300).summarize_section(doc, section)
    assert usage.llm_calls == 1 and again.json_modes == [True]
