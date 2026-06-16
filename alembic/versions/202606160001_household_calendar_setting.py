"""household calendar setting

Revision ID: 202606160001
Revises: 202605210002
Create Date: 2026-06-16 18:55:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.config import get_settings


revision: str = "202606160001"
down_revision: str | None = "202605210002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("households", sa.Column("google_calendar_id", sa.String(length=255), nullable=True))
    default_calendar_id = get_settings().google_calendar_id.strip()
    if default_calendar_id and default_calendar_id != "primary":
        op.execute(
            sa.text("update households set google_calendar_id = :calendar_id where google_calendar_id is null")
            .bindparams(calendar_id=default_calendar_id)
        )
        op.execute(
            sa.text(
                """
                update calendar_connections
                set external_account_id = :calendar_id
                where provider = 'google'
                  and (external_account_id is null or external_account_id = 'primary')
                """
            ).bindparams(calendar_id=default_calendar_id)
        )


def downgrade() -> None:
    op.drop_column("households", "google_calendar_id")
