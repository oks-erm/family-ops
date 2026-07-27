"""track students hidden from scheduling management

Revision ID: 202607270003
Revises: 202607270002
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607270003"
down_revision: str | None = "202607270002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hidden_scheduling_students",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("student_email", sa.String(length=320), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["scheduling_profiles.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id",
            "student_email",
            name="uq_hidden_scheduling_student_profile_email",
        ),
    )
    op.create_index(
        "ix_hidden_scheduling_students_profile_id",
        "hidden_scheduling_students",
        ["profile_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_hidden_scheduling_students_profile_id",
        table_name="hidden_scheduling_students",
    )
    op.drop_table("hidden_scheduling_students")
