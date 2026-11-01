import asyncio
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from chapterwise import delivery
from chapterwise.db import Delivery, DocumentRow, Job, Subscription
from chapterwise.links import read_token_target
from chapterwise.worker import Worker
from tests.conftest import FakeLLM

ROME = ZoneInfo("Europe/Rome")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=ROME)


class FakeSender:
    def __init__(self):
        self.messages: list[tuple[str, list]] = []
        self.notes: list[str] = []

    async def send(self, html, buttons):
        self.messages.append((html, buttons))
        return 1000 + len(self.messages)

    async def notify(self, text):
        self.notes.append(text)


@pytest.fixture
def tg_settings(settings):
    return replace(
        settings, telegram_token="123:abc", telegram_chat_id=42, public_url="https://read.example.com"
    )


@pytest.fixture
def worker(sessions, tg_settings):
    w = Worker(tg_settings, sessions, llm=FakeLLM("fake-model"))
    w.schedule_scan()
    while w.run_once():
        pass
    return w


@pytest.fixture
def epub(sessions, worker) -> int:
    with sessions() as s:
        return s.scalar(select(DocumentRow.id).where(DocumentRow.format == "epub"))


def drain(worker):
    while worker.run_once():
        pass


def deliver(sessions, settings, now) -> FakeSender:
    sender = FakeSender()
    asyncio.run(delivery.deliver_due(sessions, settings, sender, now=now))
    return sender


def test_subscribe_prefetches_and_rejects_bad_input(sessions, epub):
    with sessions() as s:
        sub = delivery.subscribe(s, epub)
        assert (sub.status, sub.position) == ("active", 0)
        queued = s.scalars(select(Job.payload).where(Job.kind == "summarize")).all()
        assert sorted(p["position"] for p in queued) == [0, 1, 2]
        assert delivery.subscribe(s, epub).id == sub.id  # one open subscription per book

        with pytest.raises(delivery.SubscriptionError):
            delivery.subscribe(s, epub, start=99)
        with pytest.raises(delivery.SubscriptionError):
            delivery.subscribe(s, 12345)


def test_daily_delivery_at_0730_once_per_day(sessions, tg_settings, worker, epub):
    with sessions() as s:
        delivery.subscribe(s, epub)
    drain(worker)  # summaries for sections 0-2

    assert deliver(sessions, tg_settings, at(2, 7, 29)).messages == []  # too early

    sender = deliver(sessions, tg_settings, at(2, 7, 30))
    assert len(sender.messages) == 1
    html, buttons = sender.messages[0]
    assert html.startswith("<b>The Test Book</b>\nChapter 1 · 1/3 · ⏱ 1 min")
    assert "💡 <b>TL;DR</b>\nsummary #" in html
    assert "🧠 <b>Attention is limited</b>\nYou <b>can't</b> focus on everything." in html
    assert "🎯 <b>Takeaway:</b>" in html
    ((label, target),) = buttons[0]
    assert label == "📖 Read the article" and target.startswith("app:https://read.example.com/r/")
    assert read_token_target(tg_settings.secret_key, target.rsplit("/", 1)[1]) == (epub, 0)
    assert buttons[1] == [("Next ▶", "cb:next:1"), ("Pause", "cb:pause:1")]

    assert deliver(sessions, tg_settings, at(2, 12)).messages == []  # already delivered today
    assert len(deliver(sessions, tg_settings, at(3, 7, 31)).messages) == 1  # next morning

    with sessions() as s:
        sub = s.scalar(select(Subscription))
        assert sub.position == 2 and str(sub.last_delivered_on) == "2026-11-03"
        assert [d.message_id for d in s.scalars(select(Delivery).order_by(Delivery.id))] == [1001, 1001]


def test_missed_morning_is_caught_up(sessions, tg_settings, worker, epub):
    with sessions() as s:
        delivery.subscribe(s, epub)
    drain(worker)
    # The bot was down at 07:30; the first check afterwards still delivers today's section.
    assert len(deliver(sessions, tg_settings, at(2, 21, 15)).messages) == 1


def test_waits_for_summary_then_delivers(sessions, tg_settings, worker, epub):
    with sessions() as s:
        delivery.subscribe(s, epub)
    assert deliver(sessions, tg_settings, at(2, 8)).messages == []  # summary not ready yet
    drain(worker)
    assert len(deliver(sessions, tg_settings, at(2, 8, 1)).messages) == 1


def test_next_sends_extra_section_without_skipping_tomorrow(sessions, tg_settings, worker, epub):
    with sessions() as s:
        sub_id = delivery.subscribe(s, epub).id
    drain(worker)
    deliver(sessions, tg_settings, at(2, 7, 30))  # section 0

    with sessions() as s:
        delivery.request_next(s, sub_id)
    drain(worker)
    manual = deliver(sessions, tg_settings, at(2, 9))
    assert "Chapter 2 · 2/3" in manual.messages[0][0]

    tomorrow = deliver(sessions, tg_settings, at(3, 7, 30))
    assert "Chapter 3 · 3/3" in tomorrow.messages[0][0]
    assert tomorrow.messages[0][1][1] == [("Pause", "cb:pause:1")]  # no Next on the last section
    assert tomorrow.notes == ["🎉 You finished The Test Book! Pick the next one with /books."]
    with sessions() as s:
        assert s.get(Subscription, sub_id).status == "finished"
    assert deliver(sessions, tg_settings, at(4, 7, 30)).messages == []


def test_paused_and_stopped_get_nothing(sessions, tg_settings, worker, epub):
    with sessions() as s:
        sub_id = delivery.subscribe(s, epub).id
        delivery.set_status(s, sub_id, "paused")
    drain(worker)
    assert deliver(sessions, tg_settings, at(2, 8)).messages == []

    with sessions() as s:
        delivery.set_status(s, sub_id, "active")
    assert len(deliver(sessions, tg_settings, at(2, 8)).messages) == 1

    with sessions() as s:
        delivery.set_status(s, sub_id, "stopped")
        with pytest.raises(delivery.SubscriptionError):
            delivery.request_next(s, sub_id)
        assert delivery.subscribe(s, epub).id != sub_id  # a stopped book can be started again


def test_old_format_summary_is_still_delivered(sessions, tg_settings, epub):
    """Summaries made before the card format go out as before instead of waiting for a new one."""
    from chapterwise.cache import PostgresCache
    from chapterwise.db import SectionRow
    from chapterwise.summarize import cache_key

    with sessions() as s:
        delivery.subscribe(s, epub)
        doc = s.get(DocumentRow, epub)
        section = s.scalar(select(SectionRow).where(SectionRow.document_id == epub, SectionRow.position == 0))
        key = cache_key("section", section.text_sha256, doc.title, section.title, "fake-model", "1")
    PostgresCache(sessions).set(key, {"text": "Old **markdown** summary"})

    sender = deliver(sessions, tg_settings, at(2, 7, 30))
    html, _ = sender.messages[0]
    assert "Old <b>markdown</b> summary" in html and "⏱" not in html


def test_without_secret_key_the_button_opens_the_login_page(sessions, tg_settings, worker, epub):
    settings = replace(tg_settings, secret_key=None)
    with sessions() as s:
        delivery.subscribe(s, epub)
    drain(worker)
    _, buttons = deliver(sessions, settings, at(2, 7, 30)).messages[0]
    assert buttons[0] == [("📖 Open on the web", f"url:https://read.example.com/b/{epub}/s/0")]
