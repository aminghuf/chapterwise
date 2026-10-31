from sqlalchemy import select

from chapterwise import jobs
from chapterwise.cache import PostgresCache, cache_digest
from chapterwise.db import CacheEntry, DocumentRow, Job, SectionRow
from chapterwise.summarize import cache_key
from chapterwise.worker import Worker
from tests.conftest import FakeLLM


def drain(worker: Worker, limit: int = 50) -> int:
    ran = 0
    while worker.run_once():
        ran += 1
        assert ran < limit, "queue never emptied"
    return ran


def test_scan_parse_and_summarize_end_to_end(sessions, settings):
    llm = FakeLLM("fake-model")
    worker = Worker(settings, sessions, llm=llm)

    worker.schedule_scan()
    assert drain(worker) == 3  # scan + 2 parses
    with sessions() as s:
        epub = s.scalar(select(DocumentRow).where(DocumentRow.format == "epub"))
        assert epub.status == "ready"
        jobs.enqueue(
            s, "summarize", {"document_id": epub.id, "position": 1}, dedupe_key=f"summarize:{epub.id}:1"
        )
        s.commit()

    assert drain(worker) == 1
    assert len(llm.calls) == 1 and "beta0" in llm.calls[0]  # chapter 2's text was sent

    with sessions() as s:
        section = s.scalar(
            select(SectionRow).where(SectionRow.document_id == epub.id, SectionRow.position == 1)
        )
        key = cache_key("section", section.text_sha256, epub.title, section.title, "fake-model")
        assert s.get(CacheEntry, cache_digest(key)).value["text"] == "summary #1"
        assert {j.status for j in s.scalars(select(Job))} == {"done"}


def test_repeat_summary_uses_cache(sessions, settings):
    llm = FakeLLM("fake-model")
    worker = Worker(settings, sessions, llm=llm)
    worker.schedule_scan()
    drain(worker)
    with sessions() as s:
        doc_id = s.scalar(select(DocumentRow.id).where(DocumentRow.format == "md"))
    for _ in range(2):
        with sessions() as s:
            jobs.enqueue(s, "summarize", {"document_id": doc_id, "position": 0}, dedupe_key="x")
            s.commit()
        drain(worker)
    assert len(llm.calls) == 1


def test_missing_api_key_fails_without_retrying(sessions, settings):
    from dataclasses import replace

    worker = Worker(replace(settings, api_key=None), sessions)
    assert worker.summarizer is None
    with sessions() as s:
        jobs.enqueue(s, "summarize", {"document_id": 1, "position": 0})
        s.commit()
    worker.run_once()
    with sessions() as s:
        job = s.scalar(select(Job))
        assert job.status == "failed" and "DEEPSEEK_API_KEY" in job.error


def test_llm_errors_are_retried(sessions, settings):
    class FlakyLLM(FakeLLM):
        def complete(self, system, user):
            raise ConnectionError("network down")

    worker = Worker(settings, sessions, llm=FlakyLLM("fake-model"))
    worker.schedule_scan()
    drain(worker)
    with sessions() as s:
        doc_id = s.scalar(select(DocumentRow.id).where(DocumentRow.format == "md"))
        jobs.enqueue(s, "summarize", {"document_id": doc_id, "position": 0})
        s.commit()
    worker.run_once()
    with sessions() as s:
        job = s.scalar(select(Job).where(Job.kind == "summarize"))
        assert job.status == "queued" and job.attempts == 1 and "network down" in job.error


def test_postgres_cache_roundtrip(sessions):
    cache = PostgresCache(sessions)
    key = {"kind": "section", "text_sha256": "abc"}
    assert cache.get(key) is None
    cache.set(key, {"text": "one"})
    cache.set(key, {"text": "two"})  # overwrite, no duplicate-key error
    assert cache.get(key) == {"text": "two"}
    assert cache.existing([cache_digest(key), "f" * 64]) == {cache_digest(key)}
