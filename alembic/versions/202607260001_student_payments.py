"""student payment credits and lesson allocations

Revision ID: 202607260001
Revises: 202607250001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "202607260001"
down_revision: str | None = "202607250001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lesson_bookings",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "lesson_bookings",
        sa.Column(
            "cancellation_consumes_credit",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.create_table(
        "student_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("student_email", sa.String(length=320), nullable=False),
        sa.Column("lessons_purchased", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index("ix_student_payments_profile_id", "student_payments", ["profile_id"])
    op.create_index("ix_student_payments_student_email", "student_payments", ["student_email"])
    op.create_table(
        "lesson_payment_allocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("booking_id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.ForeignKeyConstraint(["booking_id"], ["lesson_bookings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["payment_id"], ["student_payments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("booking_id", name="uq_lesson_payment_allocation_booking"),
    )
    op.create_index(
        "ix_lesson_payment_allocations_booking_id",
        "lesson_payment_allocations",
        ["booking_id"],
    )
    op.create_index(
        "ix_lesson_payment_allocations_payment_id",
        "lesson_payment_allocations",
        ["payment_id"],
    )


def downgrade() -> None:
    op.drop_table("lesson_payment_allocations")
    op.drop_table("student_payments")
    op.drop_column("lesson_bookings", "cancellation_consumes_credit")
    op.drop_column("lesson_bookings", "cancelled_at")
