"""weekly recommendations

Revision ID: 202605200006
Revises: 202605200005
Create Date: 2026-05-20

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "202605200006"
down_revision: str | None = "202605200005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "household_recommendations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("recommendations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("household_id", "period_start", "period_end", name="uq_household_recommendation_period"),
    )
    op.create_index(op.f("ix_household_recommendations_household_id"), "household_recommendations", ["household_id"], unique=False)
    op.create_index(op.f("ix_household_recommendations_period_end"), "household_recommendations", ["period_end"], unique=False)
    op.create_index(op.f("ix_household_recommendations_period_start"), "household_recommendations", ["period_start"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_household_recommendations_period_start"), table_name="household_recommendations")
    op.drop_index(op.f("ix_household_recommendations_period_end"), table_name="household_recommendations")
    op.drop_index(op.f("ix_household_recommendations_household_id"), table_name="household_recommendations")
    op.drop_table("household_recommendations")
