"""finance activity prices

Revision ID: 202605210001
Revises: 202605200006
Create Date: 2026-05-21

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "202605210001"
down_revision: str | None = "202605200006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    transaction_type = postgresql.ENUM("expense", "income", name="transactiontype")
    activity_action = postgresql.ENUM("created", "updated", "deleted", name="activityaction")
    transaction_type.create(op.get_bind(), checkfirst=True)
    activity_action.create(op.get_bind(), checkfirst=True)
    transaction_type_column = postgresql.ENUM(
        "expense", "income", name="transactiontype", create_type=False
    )
    activity_action_column = postgresql.ENUM(
        "created", "updated", "deleted", name="activityaction", create_type=False
    )

    op.create_table(
        "financial_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("transaction_type", transaction_type_column, nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("merchant", sa.String(length=255), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("amount", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("raw_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_financial_transactions_category"), "financial_transactions", ["category"], unique=False)
    op.create_index(op.f("ix_financial_transactions_household_id"), "financial_transactions", ["household_id"], unique=False)
    op.create_index(op.f("ix_financial_transactions_occurred_on"), "financial_transactions", ["occurred_on"], unique=False)
    op.create_index(op.f("ix_financial_transactions_transaction_type"), "financial_transactions", ["transaction_type"], unique=False)
    op.create_index(op.f("ix_financial_transactions_user_id"), "financial_transactions", ["user_id"], unique=False)

    op.create_table(
        "activity_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("action", activity_action_column, nullable=False),
        sa.Column("entity_type", sa.String(length=100), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_activity_logs_action"), "activity_logs", ["action"], unique=False)
    op.create_index(op.f("ix_activity_logs_entity_id"), "activity_logs", ["entity_id"], unique=False)
    op.create_index(op.f("ix_activity_logs_entity_type"), "activity_logs", ["entity_type"], unique=False)
    op.create_index(op.f("ix_activity_logs_household_id"), "activity_logs", ["household_id"], unique=False)
    op.create_index(op.f("ix_activity_logs_user_id"), "activity_logs", ["user_id"], unique=False)

    op.create_table(
        "shopping_price_quotes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("shopping_item_id", sa.Uuid(), nullable=True),
        sa.Column("item_name", sa.String(length=255), nullable=False),
        sa.Column("store_name", sa.String(length=255), nullable=False),
        sa.Column("product_name", sa.String(length=500), nullable=True),
        sa.Column("price", sa.String(length=64), nullable=True),
        sa.Column("old_price", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("product_url", sa.Text(), nullable=True),
        sa.Column("is_promotion", sa.Boolean(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shopping_item_id"], ["shopping_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_shopping_price_quotes_fetched_at"), "shopping_price_quotes", ["fetched_at"], unique=False)
    op.create_index(op.f("ix_shopping_price_quotes_household_id"), "shopping_price_quotes", ["household_id"], unique=False)
    op.create_index(op.f("ix_shopping_price_quotes_is_promotion"), "shopping_price_quotes", ["is_promotion"], unique=False)
    op.create_index(op.f("ix_shopping_price_quotes_item_name"), "shopping_price_quotes", ["item_name"], unique=False)
    op.create_index(op.f("ix_shopping_price_quotes_shopping_item_id"), "shopping_price_quotes", ["shopping_item_id"], unique=False)
    op.create_index(op.f("ix_shopping_price_quotes_store_name"), "shopping_price_quotes", ["store_name"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_shopping_price_quotes_store_name"), table_name="shopping_price_quotes")
    op.drop_index(op.f("ix_shopping_price_quotes_shopping_item_id"), table_name="shopping_price_quotes")
    op.drop_index(op.f("ix_shopping_price_quotes_item_name"), table_name="shopping_price_quotes")
    op.drop_index(op.f("ix_shopping_price_quotes_is_promotion"), table_name="shopping_price_quotes")
    op.drop_index(op.f("ix_shopping_price_quotes_household_id"), table_name="shopping_price_quotes")
    op.drop_index(op.f("ix_shopping_price_quotes_fetched_at"), table_name="shopping_price_quotes")
    op.drop_table("shopping_price_quotes")
    op.drop_index(op.f("ix_activity_logs_user_id"), table_name="activity_logs")
    op.drop_index(op.f("ix_activity_logs_household_id"), table_name="activity_logs")
    op.drop_index(op.f("ix_activity_logs_entity_type"), table_name="activity_logs")
    op.drop_index(op.f("ix_activity_logs_entity_id"), table_name="activity_logs")
    op.drop_index(op.f("ix_activity_logs_action"), table_name="activity_logs")
    op.drop_table("activity_logs")
    op.drop_index(op.f("ix_financial_transactions_user_id"), table_name="financial_transactions")
    op.drop_index(op.f("ix_financial_transactions_transaction_type"), table_name="financial_transactions")
    op.drop_index(op.f("ix_financial_transactions_occurred_on"), table_name="financial_transactions")
    op.drop_index(op.f("ix_financial_transactions_household_id"), table_name="financial_transactions")
    op.drop_index(op.f("ix_financial_transactions_category"), table_name="financial_transactions")
    op.drop_table("financial_transactions")
    postgresql.ENUM(name="activityaction").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="transactiontype").drop(op.get_bind(), checkfirst=True)
