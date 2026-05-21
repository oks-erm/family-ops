"""initial schema

Revision ID: 202605200001
Revises:
Create Date: 2026-05-20

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "202605200001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    task_status = postgresql.ENUM("pending", "done", "skipped", "moved", name="taskstatus")
    shopping_status = postgresql.ENUM("pending", "purchased", "skipped", name="shoppingitemstatus")
    daily_plan_status = postgresql.ENUM("draft", "sent", "reviewed", name="dailyplanstatus")
    task_status_column = postgresql.ENUM(
        "pending", "done", "skipped", "moved", name="taskstatus", create_type=False
    )
    shopping_status_column = postgresql.ENUM(
        "pending", "purchased", "skipped", name="shoppingitemstatus", create_type=False
    )
    daily_plan_status_column = postgresql.ENUM(
        "draft", "sent", "reviewed", name="dailyplanstatus", create_type=False
    )

    task_status.create(op.get_bind(), checkfirst=True)
    shopping_status.create(op.get_bind(), checkfirst=True)
    daily_plan_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("last_name", sa.String(length=255), nullable=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_telegram_chat_id"), "users", ["telegram_chat_id"], unique=False)
    op.create_index(op.f("ix_users_telegram_user_id"), "users", ["telegram_user_id"], unique=True)

    op.create_table(
        "stores",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_stores_name"),
    )

    op.create_table(
        "daily_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("plan_date", sa.Date(), nullable=False),
        sa.Column("work_start", sa.Time(), nullable=True),
        sa.Column("work_end", sa.Time(), nullable=True),
        sa.Column("unusual_notes", sa.Text(), nullable=True),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", daily_plan_status_column, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "plan_date", name="uq_daily_plans_user_date"),
    )
    op.create_index(op.f("ix_daily_plans_user_id"), "daily_plans", ["user_id"], unique=False)

    op.create_table(
        "shopping_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("store_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("store_name_raw", sa.String(length=255), nullable=True),
        sa.Column("status", shopping_status_column, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_shopping_items_user_id"), "shopping_items", ["user_id"], unique=False)

    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column("status", task_status_column, nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("moved_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tasks_user_id"), "tasks", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_tasks_user_id"), table_name="tasks")
    op.drop_table("tasks")
    op.drop_index(op.f("ix_shopping_items_user_id"), table_name="shopping_items")
    op.drop_table("shopping_items")
    op.drop_index(op.f("ix_daily_plans_user_id"), table_name="daily_plans")
    op.drop_table("daily_plans")
    op.drop_table("stores")
    op.drop_index(op.f("ix_users_telegram_user_id"), table_name="users")
    op.drop_index(op.f("ix_users_telegram_chat_id"), table_name="users")
    op.drop_table("users")

    postgresql.ENUM(name="dailyplanstatus").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="shoppingitemstatus").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="taskstatus").drop(op.get_bind(), checkfirst=True)
