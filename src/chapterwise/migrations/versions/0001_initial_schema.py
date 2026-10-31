"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-01 00:16:29.587512
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("format", sa.String(length=10), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime", sa.Double(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=35), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "jobs_active_dedupe",
        "jobs",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index("jobs_queue", "jobs", ["status", "run_after"], unique=False)
    op.create_table(
        "llm_cache",
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("key", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("digest"),
    )
    op.create_table(
        "sections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(length=64), nullable=False),
        sa.Column("words", sa.Integer(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "position"),
    )
    op.create_index(op.f("ix_sections_document_id"), "sections", ["document_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_sections_document_id"), table_name="sections")
    op.drop_table("sections")
    op.drop_table("llm_cache")
    op.drop_index("jobs_queue", table_name="jobs")
    op.drop_index(
        "jobs_active_dedupe", table_name="jobs", postgresql_where=sa.text("status IN ('queued', 'running')")
    )
    op.drop_table("jobs")
    op.drop_table("documents")
