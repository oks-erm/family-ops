"""dashboard google auth

Revision ID: 202605210002
Revises: 202605210001
Create Date: 2026-05-21

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "202605210002"
down_revision: str | None = "202605210001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("google_email", sa.String(length=320), nullable=True))
    op.add_column("users", sa.Column("dashboard_link_token", sa.String(length=128), nullable=True))
    op.add_column(
        "users",
        sa.Column("dashboard_link_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(op.f("ix_users_google_email"), "users", ["google_email"], unique=True)
    op.create_index(op.f("ix_users_dashboard_link_token"), "users", ["dashboard_link_token"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_dashboard_link_token"), table_name="users")
    op.drop_index(op.f("ix_users_google_email"), table_name="users")
    op.drop_column("users", "dashboard_link_expires_at")
    op.drop_column("users", "dashboard_link_token")
    op.drop_column("users", "google_email")
