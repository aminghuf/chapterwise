"""Postgres schema and session handling."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.types import Date, DateTime

ACTIVE_JOB_STATUSES = ("queued", "running")
OPEN_SUBSCRIPTION_STATUSES = ("active", "paused")


class Base(DeclarativeBase):
    pass


class DocumentRow(Base):
    """A book file found in the books folder."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(Text, unique=True)  # relative to the books folder
    format: Mapped[str] = mapped_column(String(10))
    size: Mapped[int] = mapped_column(BigInteger)
    mtime: Mapped[float] = mapped_column(Double)
    sha256: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(35))
    # pending (waiting to be parsed) | ready | error | missing (file gone from the folder)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    sections: Mapped[list[SectionRow]] = relationship(
        back_populates="document",
        order_by="SectionRow.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SectionRow(Base):
    __tablename__ = "sections"
    __table_args__ = (UniqueConstraint("document_id", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    level: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))
    words: Mapped[int] = mapped_column(Integer)
    tokens: Mapped[int] = mapped_column(Integer)

    document: Mapped[DocumentRow] = relationship(back_populates="sections")


class Job(Base):
    """Background work: scan the library, parse a book, summarize a section."""

    __tablename__ = "jobs"
    __table_args__ = (
        # At most one queued/running job per dedupe key (e.g. the same section twice).
        Index(
            "jobs_active_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("jobs_queue", "status", "run_after"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|done|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CacheEntry(Base):
    """LLM results, content-addressed. Summaries are read back from here for display."""

    __tablename__ = "llm_cache"

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[dict[str, Any]] = mapped_column(JSONB)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Subscription(Base):
    """A book being read on Telegram, one section a day."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        # One open (active or paused) subscription per book.
        Index(
            "subscriptions_one_open_per_book",
            "document_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'paused')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="summary")  # "dose" arrives in Phase 4
    position: Mapped[int] = mapped_column(Integer, default=0)  # next section to deliver
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|paused|finished|stopped
    wants_next: Mapped[bool] = mapped_column(Boolean, default=False)  # "Next" pressed, waiting for summary
    last_delivered_on: Mapped[date | None] = mapped_column(Date)  # local date of the last daily delivery
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    document: Mapped[DocumentRow] = relationship()


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # scheduled | manual
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
