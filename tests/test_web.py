import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from chapterwise.cache import PostgresCache
from chapterwise.db import DocumentRow, Job, SectionRow
from chapterwise.summarize import cache_key
from chapterwise.web import create_app
from chapterwise.worker import Worker
from tests.conftest import FakeLLM


@pytest.fixture
def library(sessions, settings):
    """A scanned and parsed library."""
    worker = Worker(settings, sessions, llm=FakeLLM("fake-model"))
    worker.schedule_scan()
    while worker.run_once():
        pass
    return worker


@pytest.fixture
def client(sessions, settings):
    return TestClient(create_app(settings, sessions))


@pytest.fixture
def logged_in(client):
    response = client.post("/login", data={"password": "correct horse", "next": "/"}, follow_redirects=False)
    assert response.status_code == 303
    return client


def epub_id(sessions) -> int:
    with sessions() as s:
        return s.scalar(select(DocumentRow.id).where(DocumentRow.format == "epub"))


def test_healthz_is_public(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_pages_require_login(client):
    response = client.get("/b/1?x=1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/b/1%3Fx%3D1"


def test_wrong_password_then_lockout(client):
    for _ in range(10):
        assert "Wrong password" in client.post("/login", data={"password": "nope"}).text
    blocked = client.post("/login", data={"password": "correct horse"})
    assert "Too many attempts" in blocked.text


def test_login_redirect_stays_on_site(client):
    response = client.post(
        "/login", data={"password": "correct horse", "next": "//evil.example"}, follow_redirects=False
    )
    assert response.headers["location"] == "/"


def test_cross_site_post_is_blocked(logged_in):
    response = logged_in.post("/rescan", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_library_lists_books(logged_in, library):
    page = logged_in.get("/")
    assert page.status_code == 200
    assert "The Test Book" in page.text and "Ada Tester" in page.text and "Playbook" in page.text
    assert page.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert "The Test Book" in logged_in.get("/?q=ada").text
    assert "The Test Book" not in logged_in.get("/?q=playbook").text


def test_summarize_flow(logged_in, library, sessions):
    doc_id = epub_id(sessions)
    book = logged_in.get(f"/b/{doc_id}")
    assert "Chapter 2" in book.text and "Summarize all remaining (3)" in book.text

    response = logged_in.post(f"/b/{doc_id}/summarize", data={"sections": "1"}, follow_redirects=False)
    assert response.headers["location"] == f"/b/{doc_id}#s1"
    assert "queued" in logged_in.get(f"/b/{doc_id}").text
    assert 'http-equiv="refresh"' in logged_in.get(f"/b/{doc_id}/s/1").text

    while library.run_once():
        pass
    section = logged_in.get(f"/b/{doc_id}/s/1")
    assert "summary #1" in section.text
    assert "Chapter 1" in section.text and "Chapter 3" in section.text  # prev / next links

    logged_in.post(f"/b/{doc_id}/summarize", data={"sections": "all"})
    with sessions() as s:
        queued = s.scalars(select(Job).where(Job.kind == "summarize", Job.status == "queued")).all()
        assert sorted(j.payload["position"] for j in queued) == [0, 2]  # chapter 2 is already done


def test_summary_html_is_escaped(logged_in, library, sessions, settings):
    doc_id = epub_id(sessions)
    with sessions() as s:
        doc = s.get(DocumentRow, doc_id)
        section = s.scalar(
            select(SectionRow).where(SectionRow.document_id == doc_id, SectionRow.position == 0)
        )
        key = cache_key("section", section.text_sha256, doc.title, section.title, settings.model)
    PostgresCache(sessions).set(key, {"text": "**Bold** <script>alert(1)</script>"})

    page = logged_in.get(f"/b/{doc_id}/s/0").text
    assert "<strong>Bold</strong>" in page
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_unknown_book_and_section(logged_in, library, sessions):
    assert logged_in.get("/b/999").status_code == 404
    assert logged_in.get(f"/b/{epub_id(sessions)}/s/99").status_code == 404


def test_jobs_page_and_retry(logged_in, library, sessions):
    with sessions() as s:
        s.add(Job(kind="summarize", payload={"document_id": 1, "position": 0}, status="failed", error="boom"))
        s.commit()
        job_id = s.scalar(select(Job.id).where(Job.status == "failed"))
    page = logged_in.get("/jobs").text
    assert "boom" in page and "Retry" in page
    logged_in.post(f"/jobs/{job_id}/retry")
    with sessions() as s:
        assert s.get(Job, job_id).status == "queued"


def test_logout(logged_in):
    logged_in.post("/logout")
    assert logged_in.get("/", follow_redirects=False).status_code == 303


def test_telegram_subscription_controls(library, sessions, settings):
    from dataclasses import replace

    client = TestClient(create_app(replace(settings, telegram_token="123:abc"), sessions))
    client.post("/login", data={"password": "correct horse"})
    doc_id = epub_id(sessions)
    assert "Read daily on Telegram" in client.get(f"/b/{doc_id}").text

    client.post(f"/b/{doc_id}/subscribe", data={"start": "0"})
    page = client.get(f"/b/{doc_id}").text
    assert "Daily on Telegram at 07:30" in page and "Pause" in page

    with sessions() as s:
        from chapterwise.db import Subscription

        sub_id = s.scalar(select(Subscription.id))
    client.post(f"/subscriptions/{sub_id}/pause")
    assert "paused" in client.get(f"/b/{doc_id}").text
    client.post(f"/subscriptions/{sub_id}/stop")
    assert "Read daily on Telegram" in client.get(f"/b/{doc_id}").text
    assert client.post(f"/subscriptions/{sub_id}/explode").status_code == 404


def test_subscription_controls_hidden_without_telegram(logged_in, library, sessions):
    assert "Read daily on Telegram" not in logged_in.get(f"/b/{epub_id(sessions)}").text
