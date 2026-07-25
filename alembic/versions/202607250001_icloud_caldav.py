"""add iCloud CalDAV calendar provider

Revision ID: 202607250001
Revises: 202607240002
Create Date: 2026-07-25 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "202607250001"
down_revision: str | None = "202607240002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE calendarprovider ADD VALUE IF NOT EXISTS 'icloud'")


def downgrade() -> None:
    op.execute("DELETE FROM calendar_events_cache WHERE source_type = 'icloud'")
    op.execute(
        """
        DELETE FROM scheduling_calendars
        WHERE connection_id IN (
            SELECT id FROM calendar_connections WHERE provider = 'icloud'
        )
        """
    )
    op.execute("DELETE FROM calendar_connections WHERE provider = 'icloud'")
    op.execute(
        "ALTER TABLE calendar_connections ALTER COLUMN provider TYPE VARCHAR "
        "USING provider::text"
    )
    op.execute(
        "ALTER TABLE calendar_events_cache ALTER COLUMN source_type TYPE VARCHAR "
        "USING source_type::text"
    )
    op.execute("DROP TYPE calendarprovider")
    op.execute("CREATE TYPE calendarprovider AS ENUM ('google', 'ical')")
    op.execute(
        "ALTER TABLE calendar_connections ALTER COLUMN provider TYPE calendarprovider "
        "USING provider::calendarprovider"
    )
    op.execute(
        "ALTER TABLE calendar_events_cache ALTER COLUMN source_type TYPE calendarprovider "
        "USING source_type::calendarprovider"
    )
