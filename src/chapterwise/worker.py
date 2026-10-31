"""The background worker: scans the library on a timer and runs queued jobs one at a time."""

from __future__ import annotations

import logging
import signal
import threading
import time

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from chapterwise import jobs
from chapterwise.cache import PostgresCache
from chapterwise.config import Settings
from chapterwise.db import DocumentRow, Job, SectionRow
from chapterwise.library import parse_document, scan, to_models
from chapterwise.llm import LLM, OpenAICompatibleLLM
from chapterwise.summarize import Summarizer

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0


class PermanentError(Exception):
    """A job that can't succeed by retrying (missing book, no API key...)."""


class Worker:
    def __init__(self, settings: Settings, sessions: sessionmaker, llm: LLM | None = None):
        self.settings = settings
        self.sessions = sessions
        if llm is None and settings.api_key:
            llm = OpenAICompatibleLLM(settings.api_key, settings.base_url, settings.model)
        self.summarizer = (
            Summarizer(llm, PostgresCache(sessions), settings.max_chunk_tokens) if llm is not None else None
        )
        self._stop = threading.Event()
        self._last_scan = float("-inf")

    def stop(self, *_args) -> None:
        log.info("Stopping after the current job")
        self._stop.set()

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        with self.sessions() as session:
            if requeued := jobs.requeue_stale(session):
                log.info("Requeued %d job(s) interrupted by the last shutdown", requeued)
        log.info("Worker started (books: %s, model: %s)", self.settings.books_dir, self.settings.model)
        while not self._stop.is_set():
            self.schedule_scan()
            if not self.run_once():
                self._stop.wait(POLL_SECONDS)

    def schedule_scan(self) -> None:
        now = time.monotonic()
        if now - self._last_scan >= self.settings.scan_interval:
            with self.sessions() as session:
                jobs.enqueue(session, "scan", dedupe_key="scan")
                session.commit()
            self._last_scan = now

    def run_once(self) -> bool:
        """Run one job if there is one. Returns False when the queue is empty."""
        with self.sessions() as session:
            job = jobs.claim(session)
        if job is None:
            return False

        started = time.monotonic()
        try:
            self.handle(job)
        except PermanentError as exc:
            with self.sessions() as session:
                jobs.fail(session, job.id, str(exc), retry=False)
            log.error("Job %d (%s) failed: %s", job.id, job.kind, exc)
        except Exception as exc:
            with self.sessions() as session:
                status = jobs.fail(session, job.id, f"{type(exc).__name__}: {exc}")
            log.exception("Job %d (%s) failed, now %s", job.id, job.kind, status)
        else:
            with self.sessions() as session:
                jobs.complete(session, job.id)
            log.info("Job %d (%s) done in %.1fs", job.id, job.kind, time.monotonic() - started)
        return True

    def handle(self, job: Job) -> None:
        if job.kind == "scan":
            with self.sessions() as session:
                result = scan(session, self.settings.books_dir)
            log.info("Scan: %s", result)
        elif job.kind == "parse":
            with self.sessions() as session:
                parse_document(session, self.settings.books_dir, job.payload["document_id"])
        elif job.kind == "summarize":
            self.summarize(job.payload["document_id"], job.payload["position"])
        else:
            raise PermanentError(f"Unknown job kind '{job.kind}'")

    def summarize(self, document_id: int, position: int) -> None:
        if self.summarizer is None:
            raise PermanentError("DEEPSEEK_API_KEY is not set")
        with self.sessions() as session:
            doc = session.get(DocumentRow, document_id)
            section = session.scalar(
                select(SectionRow).where(
                    SectionRow.document_id == document_id, SectionRow.position == position
                )
            )
            if doc is None or section is None:
                raise PermanentError(f"Section {position} of document {document_id} no longer exists")
            model_doc, model_section = to_models(doc, section)
        _, usage = self.summarizer.summarize_section(model_doc, model_section)
        log.info(
            "Summarized '%s' / '%s': %d call(s), %d cached, %d tokens in, %d out",
            model_doc.title,
            model_section.title,
            usage.llm_calls,
            usage.cache_hits,
            usage.tokens_in,
            usage.tokens_out,
        )
