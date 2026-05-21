"""planning and calendar

Revision ID: 202605200005
Revises: 202605200004
Create Date: 2026-05-20

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "202605200005"
down_revision: str | None = "202605200004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    planning_state = postgresql.ENUM(
        "awaiting_work_start",
        "awaiting_work_end",
        "awaiting_unusual_notes",
        "complete",
        name="planningconversationstate",
    )
    calendar_provider = postgresql.ENUM("google", "ical", name="calendarprovider")
    planning_state.create(op.get_bind(), checkfirst=True)
    calendar_provider.create(op.get_bind(), checkfirst=True)

    planning_state_column = postgresql.ENUM(
        "awaiting_work_start",
        "awaiting_work_end",
        "awaiting_unusual_notes",
        "complete",
        name="planningconversationstate",
        create_type=False,
    )
    calendar_provider_column = postgresql.ENUM(
        "google", "ical", name="calendarprovider", create_type=False
    )
    task_status_column = postgresql.ENUM(
        "pending", "done", "skipped", "moved", name="taskstatus", create_type=False
    )

    op.add_column("tasks", sa.Column("household_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_tasks_household_id_households",
        "tasks",
        "households",
        ["household_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_tasks_household_id"), "tasks", ["household_id"], unique=False)
    op.execute(
        """
        update tasks t
        set household_id = hm.household_id
        from household_members hm
        where t.user_id = hm.user_id and t.household_id is null
        """
    )

    op.add_column("daily_plans", sa.Column("household_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_daily_plans_household_id_households",
        "daily_plans",
        "households",
        ["household_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_daily_plans_household_id"), "daily_plans", ["household_id"], unique=False)
    op.execute(
        """
        update daily_plans dp
        set household_id = hm.household_id
        from household_members hm
        where dp.user_id = hm.user_id and dp.household_id is null
        """
    )

    op.create_table(
        "task_completions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=True),
        sa.Column("completed_on", sa.Date(), nullable=False),
        sa.Column("status", task_status_column, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_task_completions_completed_on"), "task_completions", ["completed_on"], unique=False)
    op.create_index(op.f("ix_task_completions_household_id"), "task_completions", ["household_id"], unique=False)
    op.create_index(op.f("ix_task_completions_task_id"), "task_completions", ["task_id"], unique=False)
    op.create_index(op.f("ix_task_completions_user_id"), "task_completions", ["user_id"], unique=False)

    op.create_table(
        "planning_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=True),
        sa.Column("plan_date", sa.Date(), nullable=False),
        sa.Column("state", planning_state_column, nullable=False),
        sa.Column("work_start", sa.Time(), nullable=True),
        sa.Column("work_end", sa.Time(), nullable=True),
        sa.Column("unusual_notes", sa.Text(), nullable=True),
        sa.Column("raw_notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "plan_date", name="uq_planning_conversations_user_date"),
    )
    op.create_index(op.f("ix_planning_conversations_household_id"), "planning_conversations", ["household_id"], unique=False)
    op.create_index(op.f("ix_planning_conversations_user_id"), "planning_conversations", ["user_id"], unique=False)

    op.create_table(
        "routines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("schedule", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_routines_household_id"), "routines", ["household_id"], unique=False)

    op.create_table(
        "calendar_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=True),
        sa.Column("provider", calendar_provider_column, nullable=False),
        sa.Column("external_account_id", sa.String(length=255), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_calendar_connections_household_id"), "calendar_connections", ["household_id"], unique=False)
    op.create_index(op.f("ix_calendar_connections_user_id"), "calendar_connections", ["user_id"], unique=False)

    op.create_table(
        "ical_feeds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ical_feeds_household_id"), "ical_feeds", ["household_id"], unique=False)
    op.create_index(op.f("ix_ical_feeds_user_id"), "ical_feeds", ["user_id"], unique=False)

    op.create_table(
        "calendar_events_cache",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("source_type", calendar_provider_column, nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("external_event_id", sa.String(length=500), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("location", sa.String(length=500), nullable=True),
        sa.Column("raw_event", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_type", "source_id", "external_event_id", name="uq_calendar_event_source"),
    )
    op.create_index(op.f("ix_calendar_events_cache_ends_at"), "calendar_events_cache", ["ends_at"], unique=False)
    op.create_index(op.f("ix_calendar_events_cache_household_id"), "calendar_events_cache", ["household_id"], unique=False)
    op.create_index(op.f("ix_calendar_events_cache_source_id"), "calendar_events_cache", ["source_id"], unique=False)
    op.create_index(op.f("ix_calendar_events_cache_starts_at"), "calendar_events_cache", ["starts_at"], unique=False)
    op.create_index(op.f("ix_calendar_events_cache_user_id"), "calendar_events_cache", ["user_id"], unique=False)

    op.create_table(
        "scheduled_jobs_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_name", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("household_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_scheduled_jobs_log_household_id"), "scheduled_jobs_log", ["household_id"], unique=False)
    op.create_index(op.f("ix_scheduled_jobs_log_job_name"), "scheduled_jobs_log", ["job_name"], unique=False)
    op.create_index(op.f("ix_scheduled_jobs_log_user_id"), "scheduled_jobs_log", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_scheduled_jobs_log_user_id"), table_name="scheduled_jobs_log")
    op.drop_index(op.f("ix_scheduled_jobs_log_job_name"), table_name="scheduled_jobs_log")
    op.drop_index(op.f("ix_scheduled_jobs_log_household_id"), table_name="scheduled_jobs_log")
    op.drop_table("scheduled_jobs_log")
    op.drop_index(op.f("ix_calendar_events_cache_user_id"), table_name="calendar_events_cache")
    op.drop_index(op.f("ix_calendar_events_cache_starts_at"), table_name="calendar_events_cache")
    op.drop_index(op.f("ix_calendar_events_cache_source_id"), table_name="calendar_events_cache")
    op.drop_index(op.f("ix_calendar_events_cache_household_id"), table_name="calendar_events_cache")
    op.drop_index(op.f("ix_calendar_events_cache_ends_at"), table_name="calendar_events_cache")
    op.drop_table("calendar_events_cache")
    op.drop_index(op.f("ix_ical_feeds_user_id"), table_name="ical_feeds")
    op.drop_index(op.f("ix_ical_feeds_household_id"), table_name="ical_feeds")
    op.drop_table("ical_feeds")
    op.drop_index(op.f("ix_calendar_connections_user_id"), table_name="calendar_connections")
    op.drop_index(op.f("ix_calendar_connections_household_id"), table_name="calendar_connections")
    op.drop_table("calendar_connections")
    op.drop_index(op.f("ix_routines_household_id"), table_name="routines")
    op.drop_table("routines")
    op.drop_index(op.f("ix_planning_conversations_user_id"), table_name="planning_conversations")
    op.drop_index(op.f("ix_planning_conversations_household_id"), table_name="planning_conversations")
    op.drop_table("planning_conversations")
    op.drop_index(op.f("ix_task_completions_user_id"), table_name="task_completions")
    op.drop_index(op.f("ix_task_completions_task_id"), table_name="task_completions")
    op.drop_index(op.f("ix_task_completions_household_id"), table_name="task_completions")
    op.drop_index(op.f("ix_task_completions_completed_on"), table_name="task_completions")
    op.drop_table("task_completions")
    op.drop_index(op.f("ix_daily_plans_household_id"), table_name="daily_plans")
    op.drop_constraint("fk_daily_plans_household_id_households", "daily_plans", type_="foreignkey")
    op.drop_column("daily_plans", "household_id")
    op.drop_index(op.f("ix_tasks_household_id"), table_name="tasks")
    op.drop_constraint("fk_tasks_household_id_households", "tasks", type_="foreignkey")
    op.drop_column("tasks", "household_id")
    postgresql.ENUM(name="calendarprovider").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="planningconversationstate").drop(op.get_bind(), checkfirst=True)
