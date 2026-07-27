"""default legacy scheduling profiles to 30-minute commute buffers

Revision ID: 202607270002
Revises: 202607270001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "202607270002"
down_revision: str | None = "202607270001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE scheduling_profiles
            SET buffer_before_minutes = 30,
                buffer_after_minutes = 30
            WHERE buffer_before_minutes = 0
              AND buffer_after_minutes = 0
            """
        )
    )
    op.alter_column(
        "scheduling_profiles",
        "buffer_before_minutes",
        server_default=sa.text("30"),
    )
    op.alter_column(
        "scheduling_profiles",
        "buffer_after_minutes",
        server_default=sa.text("30"),
    )


def downgrade() -> None:
    # Do not overwrite values that tutors may have changed after this migration.
    op.alter_column(
        "scheduling_profiles",
        "buffer_after_minutes",
        server_default=None,
    )
    op.alter_column(
        "scheduling_profiles",
        "buffer_before_minutes",
        server_default=None,
    )
