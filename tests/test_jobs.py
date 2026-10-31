from datetime import UTC, datetime, timedelta

from chapterwise import jobs
from chapterwise.db import Job


def test_enqueue_dedupes_active_jobs(sessions):
    with sessions() as s:
        assert jobs.enqueue(s, "summarize", {"position": 1}, dedupe_key="summarize:1:1")
        assert not jobs.enqueue(s, "summarize", {"position": 1}, dedupe_key="summarize:1:1")
        assert jobs.enqueue(s, "summarize", {"position": 2}, dedupe_key="summarize:1:2")
        assert jobs.enqueue(s, "scan")  # no dedupe key: always queued
        assert jobs.enqueue(s, "scan")
        s.commit()
        assert s.query(Job).count() == 4


def test_finished_job_can_be_queued_again(sessions):
    with sessions() as s:
        jobs.enqueue(s, "scan", dedupe_key="scan")
        s.commit()
        job = jobs.claim(s)
        jobs.complete(s, job.id)
        assert jobs.enqueue(s, "scan", dedupe_key="scan")


def test_claim_takes_oldest_and_two_workers_never_share(sessions):
    with sessions() as s:
        for n in range(3):
            jobs.enqueue(s, "scan", {"n": n})
        s.commit()

    with sessions() as a, sessions() as b:
        first = jobs.claim(a)
        second = jobs.claim(b)
        assert first.payload == {"n": 0} and first.status == "running" and first.attempts == 1
        assert second.payload == {"n": 1}
    with sessions() as s:
        assert jobs.claim(s).payload == {"n": 2}
        assert jobs.claim(s) is None


def test_skip_locked(sessions):
    with sessions() as s:
        jobs.enqueue(s, "scan", {"n": 0})
        jobs.enqueue(s, "scan", {"n": 1})
        s.commit()
    # A transaction holding a row lock (as a worker mid-claim would) doesn't block the next claim.
    with sessions() as holder, sessions() as other:
        holder.execute(Job.__table__.select().where(Job.id == 1).with_for_update())
        assert jobs.claim(other).payload == {"n": 1}
        holder.rollback()


def test_failure_retries_with_backoff_then_gives_up(sessions):
    with sessions() as s:
        jobs.enqueue(s, "summarize", dedupe_key="k")
        s.commit()
        for attempt in range(1, jobs.MAX_ATTEMPTS + 1):
            s.execute(Job.__table__.update().values(run_after=datetime.now(UTC) - timedelta(seconds=1)))
            s.commit()
            job = jobs.claim(s)
            assert job.attempts == attempt
            status = jobs.fail(s, job.id, "boom")
            assert status == ("failed" if attempt == jobs.MAX_ATTEMPTS else "queued")
            if status == "queued":
                assert s.get(Job, job.id).run_after > datetime.now(UTC)  # backoff
        assert s.get(Job, job.id).error == "boom"


def test_permanent_failure_and_manual_retry(sessions):
    with sessions() as s:
        jobs.enqueue(s, "summarize", dedupe_key="k")
        s.commit()
        job = jobs.claim(s)
        assert jobs.fail(s, job.id, "no key", retry=False) == "failed"
        assert jobs.retry(s, job.id)
        refreshed = s.get(Job, job.id)
        assert (refreshed.status, refreshed.attempts, refreshed.error) == ("queued", 0, None)
        assert not jobs.retry(s, job.id)  # only failed jobs


def test_requeue_stale(sessions):
    with sessions() as s:
        jobs.enqueue(s, "scan")
        s.commit()
        jobs.claim(s)
        assert jobs.requeue_stale(s) == 1
        assert jobs.claim(s) is not None
