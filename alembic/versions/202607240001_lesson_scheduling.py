"""lesson scheduling

Revision ID: 202607240001
Revises: 202606160001
Create Date: 2026-07-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607240001"
down_revision: str | None = "202606160001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_connections",
        sa.Column("account_email", sa.String(length=320), nullable=True),
    )
    op.create_unique_constraint(
        "uq_calendar_connection_user_provider_account",
        "calendar_connections",
        ["user_id", "provider", "account_email"],
    )
    op.create_table(
        "scheduling_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("minimum_notice_minutes", sa.Integer(), nullable=False),
        sa.Column("booking_window_days", sa.Integer(), nullable=False),
        sa.Column("buffer_before_minutes", sa.Integer(), nullable=False),
        sa.Column("buffer_after_minutes", sa.Integer(), nullable=False),
        sa.Column("slot_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("booking_calendar_id", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_scheduling_profiles_slug"),
        sa.UniqueConstraint("user_id", name="uq_scheduling_profiles_user"),
    )
    op.create_index("ix_scheduling_profiles_household_id", "scheduling_profiles", ["household_id"])
    op.create_index("ix_scheduling_profiles_slug", "scheduling_profiles", ["slug"])
    op.create_index("ix_scheduling_profiles_user_id", "scheduling_profiles", ["user_id"])

    op.create_table(
        "scheduling_calendars",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("external_calendar_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("access_role", sa.String(length=32), nullable=True),
        sa.Column("include_in_conflicts", sa.Boolean(), nullable=False),
        sa.Column("can_write", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["connection_id"], ["calendar_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["scheduling_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id",
            "connection_id",
            "external_calendar_id",
            name="uq_scheduling_calendar_source",
        ),
    )
    op.create_index(
        "ix_scheduling_calendars_connection_id", "scheduling_calendars", ["connection_id"]
    )
    op.create_index("ix_scheduling_calendars_profile_id", "scheduling_calendars", ["profile_id"])

    op.create_table(
        "availability_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.Time(), nullable=False),
        sa.Column("ends_at", sa.Time(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["scheduling_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_availability_rules_profile_id", "availability_rules", ["profile_id"])
    op.create_index("ix_availability_rules_weekday", "availability_rules", ["weekday"])

    op.create_table(
        "lesson_types",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("location", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["scheduling_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lesson_types_profile_id", "lesson_types", ["profile_id"])

    op.create_table(
        "lesson_bookings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("lesson_type_id", sa.Uuid(), nullable=False),
        sa.Column("student_name", sa.String(length=255), nullable=False),
        sa.Column("student_email", sa.String(length=320), nullable=False),
        sa.Column("student_timezone", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("external_calendar_id", sa.String(length=255), nullable=True),
        sa.Column("external_event_id", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["lesson_type_id"], ["lesson_types.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["profile_id"], ["scheduling_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("profile_id", "starts_at", name="uq_lesson_booking_profile_start"),
    )
    op.create_index("ix_lesson_bookings_ends_at", "lesson_bookings", ["ends_at"])
    op.create_index("ix_lesson_bookings_lesson_type_id", "lesson_bookings", ["lesson_type_id"])
    op.create_index("ix_lesson_bookings_profile_id", "lesson_bookings", ["profile_id"])
    op.create_index("ix_lesson_bookings_starts_at", "lesson_bookings", ["starts_at"])
    op.create_index("ix_lesson_bookings_status", "lesson_bookings", ["status"])


def downgrade() -> None:
    op.drop_table("lesson_bookings")
    op.drop_table("lesson_types")
    op.drop_table("availability_rules")
    op.drop_table("scheduling_calendars")
    op.drop_table("scheduling_profiles")
    op.drop_constraint(
        "uq_calendar_connection_user_provider_account",
        "calendar_connections",
        type_="unique",
    )
    op.drop_column("calendar_connections", "account_email")
