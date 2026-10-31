"""A small job queue in Postgres. Workers claim jobs with FOR UPDATE SKIP LOCKED, so no Redis is needed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from chapterwise.db import ACTIVE_JOB_STATUSES, Job

MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 30


def enqueue(
    session: Session, kind: str, payload: dict[str, Any] | None = None, dedupe_key: str | None = None
) -> bool:
    """Queue a job. Returns False if an identical job (same dedupe_key) is already queued or running."""
    stmt = (
        insert(Job)
        .values(kind=kind, payload=payload or {}, dedupe_key=dedupe_key, status="queued")
        .on_conflict_do_nothing(
            index_elements=[Job.dedupe_key], index_where=Job.status.in_(ACTIVE_JOB_STATUSES)
        )
        .returning(Job.id)
    )
    return session.execute(stmt).first() is not None


def claim(session: Session) -> Job | None:
    """Take the oldest runnable job and mark it running. Safe with several workers."""
    next_id = (
        select(Job.id)
        .where(Job.status == "queued", Job.run_after <= func.now())
        .order_by(Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        update(Job)
        .where(Job.id == next_id)
        .values(status="running", started_at=func.now(), attempts=Job.attempts + 1)
        .returning(Job)
    )
    job = session.scalars(stmt).first()
    session.commit()
    return job


def complete(session: Session, job_id: int) -> None:
    session.execute(
        update(Job).where(Job.id == job_id).values(status="done", error=None, finished_at=func.now())
    )
    session.commit()


def fail(session: Session, job_id: int, error: str, retry: bool = True) -> str:
    """Record a failure. Retries with exponential backoff until MAX_ATTEMPTS. Returns the new status."""
    job = session.get(Job, job_id)
    if job is None:
        return "missing"
    if retry and job.attempts < MAX_ATTEMPTS:
        job.status = "queued"
        job.run_after = datetime.now(UTC) + timedelta(seconds=RETRY_BASE_SECONDS * 2 ** (job.attempts - 1))
    else:
        job.status = "failed"
        job.finished_at = datetime.now(UTC)
    job.error = error[:2000]
    session.commit()
    return job.status


def requeue_stale(session: Session) -> int:
    """Jobs left 'running' by a worker that died (e.g. a restart) go back to the queue."""
    result = session.execute(update(Job).where(Job.status == "running").values(status="queued"))
    session.commit()
    return result.rowcount


def retry(session: Session, job_id: int) -> bool:
    """Manually requeue a failed job (from the UI)."""
    job = session.get(Job, job_id)
    if job is None or job.status != "failed":
        return False
    active = session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.dedupe_key == job.dedupe_key, Job.status.in_(ACTIVE_JOB_STATUSES))
    )
    if job.dedupe_key and active:
        return False
    job.status, job.attempts, job.error, job.run_after = "queued", 0, None, datetime.now(UTC)
    session.commit()
    return True
