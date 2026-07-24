"""student Google Meet rooms

Revision ID: 202607240002
Revises: 202607240001
Create Date: 2026-07-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "202607240002"
down_revision: str | None = "202607240001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lesson_bookings",
        sa.Column("meeting_url", sa.String(length=500), nullable=True),
    )
    op.create_table(
        "student_meetings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("student_email", sa.String(length=320), nullable=False),
        sa.Column("meeting_url", sa.String(length=500), nullable=False),
        sa.Column("conference_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            name="uq_student_meeting_profile_email",
        ),
    )
    op.create_index(
        "ix_student_meetings_profile_id",
        "student_meetings",
        ["profile_id"],
    )


def downgrade() -> None:
    op.drop_table("student_meetings")
    op.drop_column("lesson_bookings", "meeting_url")
