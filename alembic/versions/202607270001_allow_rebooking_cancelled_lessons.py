"""allow rebooking cancelled lesson times

Revision ID: 202607270001
Revises: 202607260001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607270001"
down_revision: str | None = "202607260001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_lesson_booking_profile_start",
        "lesson_bookings",
        type_="unique",
    )
    op.create_index(
        "uq_lesson_booking_profile_confirmed_start",
        "lesson_bookings",
        ["profile_id", "starts_at"],
        unique=True,
        postgresql_where=sa.text("status = 'confirmed'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_lesson_booking_profile_confirmed_start",
        table_name="lesson_bookings",
    )
    op.create_unique_constraint(
        "uq_lesson_booking_profile_start",
        "lesson_bookings",
        ["profile_id", "starts_at"],
    )
