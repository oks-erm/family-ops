"""pending receipts

Revision ID: 202605200003
Revises: 202605200002
Create Date: 2026-05-20

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "202605200003"
down_revision: str | None = "202605200002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE receiptstatus ADD VALUE IF NOT EXISTS 'pending_confirmation'")
    op.create_table(
        "pending_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("image_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("extraction", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pending_receipts_telegram_chat_id"), "pending_receipts", ["telegram_chat_id"], unique=False)
    op.create_index(op.f("ix_pending_receipts_user_id"), "pending_receipts", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_pending_receipts_user_id"), table_name="pending_receipts")
    op.drop_index(op.f("ix_pending_receipts_telegram_chat_id"), table_name="pending_receipts")
    op.drop_table("pending_receipts")
