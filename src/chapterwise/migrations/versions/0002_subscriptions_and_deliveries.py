"""subscriptions and deliveries

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01 00:49:10.354298
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("wants_next", sa.Boolean(), nullable=False),
        sa.Column("last_delivered_on", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_subscriptions_document_id"), "subscriptions", ["document_id"], unique=False)
    op.create_index(
        "subscriptions_one_open_per_book",
        "subscriptions",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'paused')"),
    )
    op.create_table(
        "deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_deliveries_subscription_id"), "deliveries", ["subscription_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_deliveries_subscription_id"), table_name="deliveries")
    op.drop_table("deliveries")
    op.drop_index(
        "subscriptions_one_open_per_book",
        table_name="subscriptions",
        postgresql_where=sa.text("status IN ('active', 'paused')"),
    )
    op.drop_index(op.f("ix_subscriptions_document_id"), table_name="subscriptions")
    op.drop_table("subscriptions")
