"""Daily reading on Telegram: which section is due, and what happens after it is sent.

Everything here is plain database logic plus one async loop (`deliver_due`) that talks to a `Sender`.
The Telegram specifics live in bot.py, so this module is tested with a fake sender.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from html import escape
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from chapterwise import jobs
from chapterwise.cache import PostgresCache
from chapterwise.config import Settings
from chapterwise.db import OPEN_SUBSCRIPTION_STATUSES, Delivery, DocumentRow, SectionRow, Subscription
from chapterwise.links import read_url
from chapterwise.summarize import find_summary
from chapterwise.summary_format import try_parse_card
from chapterwise.telegram_format import build_card_message, build_message

log = logging.getLogger(__name__)

PREFETCH = 2  # summaries prepared ahead, so the morning message is ready on time


class SubscriptionError(Exception):
    """A user-facing reason a subscription action can't be done."""


@dataclass(frozen=True)
class Schedule:
    at: time
    tz: ZoneInfo

    @classmethod
    def from_settings(cls, settings: Settings) -> Schedule:
        hours, _, minutes = settings.delivery_time.partition(":")
        return cls(at=time(int(hours), int(minutes or 0)), tz=ZoneInfo(settings.timezone))

    def today(self, now: datetime) -> date:
        return now.astimezone(self.tz).date()

    def is_past_delivery_time(self, now: datetime) -> bool:
        return now.astimezone(self.tz).time() >= self.at

    def label(self) -> str:
        return self.at.strftime("%H:%M")


@dataclass(frozen=True)
class Item:
    subscription_id: int
    document_id: int
    book_title: str
    section_title: str
    position: int
    total: int
    summary: str
    kind: str  # scheduled | manual
    summary_format: str = "markdown"  # card | markdown

    @property
    def is_last(self) -> bool:
        return self.position + 1 >= self.total


# --- subscriptions ---------------------------------------------------------------------------------------


def section_count(session: Session, document_id: int) -> int:
    return session.scalar(select(func.count()).where(SectionRow.document_id == document_id)) or 0


def open_subscription(session: Session, document_id: int) -> Subscription | None:
    return session.scalar(
        select(Subscription).where(
            Subscription.document_id == document_id, Subscription.status.in_(OPEN_SUBSCRIPTION_STATUSES)
        )
    )


def subscribe(session: Session, document_id: int, start: int = 0) -> Subscription:
    """Start reading a book daily (or resume the open subscription for it)."""
    doc = session.get(DocumentRow, document_id)
    if doc is None or doc.status != "ready":
        raise SubscriptionError("That book isn't ready yet.")
    total = section_count(session, document_id)
    if not 0 <= start < total:
        raise SubscriptionError("That book has no such section.")
    sub = open_subscription(session, document_id)
    if sub is None:
        sub = Subscription(document_id=document_id, position=start, status="active", mode="summary")
        session.add(sub)
    else:
        sub.status = "active"
    session.flush()
    prefetch(session, sub)
    session.commit()
    return sub


def set_status(session: Session, subscription_id: int, status: str) -> Subscription:
    if status not in ("active", "paused", "stopped"):
        raise ValueError(status)
    sub = session.get(Subscription, subscription_id)
    if sub is None or sub.status not in OPEN_SUBSCRIPTION_STATUSES:
        raise SubscriptionError("That subscription has ended.")
    sub.status = status
    if status != "active":
        sub.wants_next = False
    session.commit()
    return sub


def request_next(session: Session, subscription_id: int) -> Subscription:
    """'Next' was tapped: deliver the next section as soon as its summary is ready."""
    sub = session.get(Subscription, subscription_id)
    if sub is None or sub.status not in OPEN_SUBSCRIPTION_STATUSES:
        raise SubscriptionError("That book is finished or stopped. Start it again with /books.")
    sub.status = "active"
    sub.wants_next = True
    prefetch(session, sub)
    session.commit()
    return sub


def open_subscriptions(session: Session) -> list[tuple[Subscription, int]]:
    """Active and paused subscriptions with their section counts, newest first."""
    subs = session.scalars(
        select(Subscription)
        .where(Subscription.status.in_(OPEN_SUBSCRIPTION_STATUSES))
        .order_by(Subscription.id.desc())
    ).all()
    return [(sub, section_count(session, sub.document_id)) for sub in subs]


def prefetch(session: Session, sub: Subscription, ahead: int = PREFETCH) -> None:
    """Queue summaries for the next sections so they're ready before they're due."""
    for position in range(sub.position, sub.position + ahead + 1):
        exists = session.scalar(
            select(SectionRow.id).where(
                SectionRow.document_id == sub.document_id, SectionRow.position == position
            )
        )
        if exists:
            jobs.enqueue(
                session,
                "summarize",
                {"document_id": sub.document_id, "position": position},
                dedupe_key=f"summarize:{sub.document_id}:{position}",
            )


# --- what's due ------------------------------------------------------------------------------------------


def due_items(
    session: Session, cache: PostgresCache, settings: Settings, schedule: Schedule, now: datetime
) -> list[Item]:
    """Sections to send now. Summaries that aren't ready yet are queued and picked up on a later check."""
    today = schedule.today(now)
    daily_due = schedule.is_past_delivery_time(now)
    items: list[Item] = []

    for sub in session.scalars(select(Subscription).where(Subscription.status == "active")):
        scheduled = daily_due and sub.last_delivered_on != today
        if not (scheduled or sub.wants_next):
            continue
        doc = session.get(DocumentRow, sub.document_id)
        total = section_count(session, sub.document_id)
        section = session.scalar(
            select(SectionRow).where(
                SectionRow.document_id == sub.document_id, SectionRow.position == sub.position
            )
        )
        if doc is None or section is None:  # past the end, e.g. after a re-parse shortened the book
            sub.status, sub.wants_next = "finished", False
            continue
        stored = find_summary(cache, section.text_sha256, doc.title, section.title, settings.model)
        if stored is None:
            prefetch(session, sub)
            continue
        items.append(
            Item(
                subscription_id=sub.id,
                document_id=doc.id,
                book_title=doc.title,
                section_title=section.title,
                position=sub.position,
                total=total,
                summary=stored.text,
                kind="scheduled" if scheduled else "manual",
                summary_format=stored.format,
            )
        )
    session.commit()
    return items


def record_delivery(
    session: Session, item: Item, message_id: int | None, schedule: Schedule, now: datetime
) -> Subscription:
    """Move the subscription past the section just sent and prepare the next ones."""
    sub = session.get(Subscription, item.subscription_id)
    session.add(
        Delivery(subscription_id=sub.id, position=item.position, kind=item.kind, message_id=message_id)
    )
    sub.position = item.position + 1
    sub.wants_next = False
    if item.kind == "scheduled":
        sub.last_delivered_on = schedule.today(now)
    if item.is_last:
        sub.status = "finished"
    else:
        prefetch(session, sub)
    session.commit()
    return sub


# --- sending ---------------------------------------------------------------------------------------------

Button = tuple[str, str]  # (label, "cb:<data>", "url:<https://...>" or "app:<https://...>" for a Mini App)


class Sender(Protocol):
    async def send(self, html: str, buttons: list[list[Button]]) -> int | None: ...

    async def notify(self, text: str) -> None: ...


def message_for(item: Item, settings: Settings) -> tuple[str, list[list[Button]]]:
    card = try_parse_card(item.summary) if item.summary_format == "card" else None
    meta = f"{item.position + 1}/{item.total}" + (f" · ⏱ {card.minutes} min" if card else "")
    header = (
        f"<b>{escape(item.book_title, quote=False)}</b>\n{escape(item.section_title, quote=False)} · {meta}"
    )
    html, _truncated = build_card_message(header, card) if card else build_message(header, item.summary)

    buttons: list[list[Button]] = []
    # Telegram only opens public https pages. With the secret key, the article opens inside Telegram
    # through a signed link, with no login; without it, the normal (login) page opens in the browser.
    if settings.public_url.startswith("https://"):
        if settings.secret_key:
            url = read_url(settings.public_url, settings.secret_key, item.document_id, item.position)
            buttons.append([("📖 Read the article", f"app:{url}")])
        else:
            url = f"{settings.public_url}/b/{item.document_id}/s/{item.position}"
            buttons.append([("📖 Open on the web", f"url:{url}")])
    row: list[Button] = []
    if not item.is_last:
        row.append(("Next ▶", f"cb:next:{item.subscription_id}"))
    row.append(("Pause", f"cb:pause:{item.subscription_id}"))
    buttons.append(row)
    return html, buttons


async def deliver_due(
    sessions: sessionmaker, settings: Settings, sender: Sender, now: datetime | None = None
) -> int:
    """Send everything that's due. Safe to call often: each section is sent once."""
    schedule = Schedule.from_settings(settings)
    cache = PostgresCache(sessions)
    now = now or datetime.now(schedule.tz)

    def load() -> list[Item]:
        with sessions() as session:
            return due_items(session, cache, settings, schedule, now)

    sent = 0
    for item in await asyncio.to_thread(load):
        html, buttons = message_for(item, settings)
        message_id = await sender.send(html, buttons)

        def save(item: Item = item, message_id: int | None = message_id) -> None:
            with sessions() as session:
                record_delivery(session, item, message_id, schedule, now)

        await asyncio.to_thread(save)
        sent += 1
        log.info("Delivered '%s' %d/%d (%s)", item.book_title, item.position + 1, item.total, item.kind)
        if item.is_last:
            await sender.notify(f"🎉 You finished {item.book_title}! Pick the next one with /books.")
    return sent
