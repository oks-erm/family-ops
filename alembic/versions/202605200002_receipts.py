"""receipts

Revision ID: 202605200002
Revises: 202605200001
Create Date: 2026-05-20

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "202605200002"
down_revision: str | None = "202605200001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    receipt_status = postgresql.ENUM("extracted", "extraction_failed", name="receiptstatus")
    receipt_status.create(op.get_bind(), checkfirst=True)
    receipt_status_column = postgresql.ENUM(
        "extracted", "extraction_failed", name="receiptstatus", create_type=False
    )

    op.create_table(
        "expense_categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_expense_categories_name"),
    )

    op.create_table(
        "receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("shop_name", sa.String(length=255), nullable=True),
        sa.Column("purchased_at", sa.Date(), nullable=True),
        sa.Column("total_amount", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=16), nullable=True),
        sa.Column("status", receipt_status_column, nullable=False),
        sa.Column("raw_extraction", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_receipts_user_id"), "receipts", ["user_id"], unique=False)

    op.create_table(
        "receipt_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("quantity", sa.String(length=64), nullable=True),
        sa.Column("total_amount", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["expense_categories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["receipt_id"], ["receipts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_receipt_items_receipt_id"), "receipt_items", ["receipt_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_receipt_items_receipt_id"), table_name="receipt_items")
    op.drop_table("receipt_items")
    op.drop_index(op.f("ix_receipts_user_id"), table_name="receipts")
    op.drop_table("receipts")
    op.drop_table("expense_categories")
    postgresql.ENUM(name="receiptstatus").drop(op.get_bind(), checkfirst=True)
