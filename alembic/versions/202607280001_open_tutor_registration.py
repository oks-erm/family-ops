"""add open tutor registration and configurable scheduling terms

Revision ID: 202607280001
Revises: 202607270003
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607280001"
down_revision: str | None = "202607270003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("users", "telegram_user_id", existing_type=sa.BigInteger(), nullable=True)
    op.alter_column("users", "telegram_chat_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column(
        "users",
        sa.Column(
            "family_dashboard_enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.add_column("scheduling_profiles", sa.Column("country", sa.String(100)))
    op.add_column("scheduling_profiles", sa.Column("tutoring_subjects", sa.Text()))
    op.add_column(
        "scheduling_profiles",
        sa.Column("currency", sa.String(3), server_default="EUR", nullable=False),
    )
    op.add_column(
        "scheduling_profiles",
        sa.Column("hourly_rate_cents", sa.Integer(), server_default="3000", nullable=False),
    )
    op.add_column(
        "scheduling_profiles",
        sa.Column(
            "cancellation_notice_hours", sa.Integer(), server_default="12", nullable=False
        ),
    )
    op.add_column(
        "scheduling_profiles",
        sa.Column(
            "late_cancellation_consumes_credit",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.add_column("scheduling_profiles", sa.Column("cancellation_policy_text", sa.Text()))
    op.create_table(
        "scheduling_packages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("lesson_count", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["scheduling_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id", "lesson_count", name="uq_scheduling_package_profile_lessons"
        ),
    )
    op.create_index(
        "ix_scheduling_packages_profile_id", "scheduling_packages", ["profile_id"]
    )
    op.add_column(
        "student_payments",
        sa.Column("currency", sa.String(3), server_default="EUR", nullable=False),
    )
    # Preserve the prices previously shown on every public booking page.
    op.execute(
        sa.text(
            """
            INSERT INTO scheduling_packages
                (id, profile_id, lesson_count, price_cents, is_active, sort_order)
            SELECT gen_random_uuid(), id, package.lesson_count, package.price_cents, true,
                   package.sort_order
            FROM scheduling_profiles
            CROSS JOIN (VALUES (8, 22400, 1), (12, 32400, 2), (20, 50000, 3))
                AS package(lesson_count, price_cents, sort_order)
            """
        )
    )


def downgrade() -> None:
    scheduling_only_users = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM users "
            "WHERE family_dashboard_enabled = false "
            "AND telegram_user_id IS NULL"
        )
    )
    if scheduling_only_users:
        raise RuntimeError(
            "Cannot downgrade while scheduling-only tutor accounts exist; "
            "export or migrate them first."
        )
    op.drop_column("student_payments", "currency")
    op.drop_index("ix_scheduling_packages_profile_id", table_name="scheduling_packages")
    op.drop_table("scheduling_packages")
    op.drop_column("scheduling_profiles", "cancellation_policy_text")
    op.drop_column("scheduling_profiles", "late_cancellation_consumes_credit")
    op.drop_column("scheduling_profiles", "cancellation_notice_hours")
    op.drop_column("scheduling_profiles", "hourly_rate_cents")
    op.drop_column("scheduling_profiles", "currency")
    op.drop_column("scheduling_profiles", "tutoring_subjects")
    op.drop_column("scheduling_profiles", "country")
    op.drop_column("users", "family_dashboard_enabled")
    op.alter_column("users", "telegram_chat_id", existing_type=sa.BigInteger(), nullable=False)
    op.alter_column("users", "telegram_user_id", existing_type=sa.BigInteger(), nullable=False)
