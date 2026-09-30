"""Isolate household calendar selection and add durable assistant state.

Revision ID: 202609300001
Revises: 202607280001
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "202609300001"
down_revision = "202607280001"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def identifier():
    return sa.Column("id", sa.Uuid(), primary_key=True)


def ref(name, target, *, index=False):
    return sa.Column(
        name, sa.Uuid(), sa.ForeignKey(target, ondelete="CASCADE"), nullable=False, index=index
    )


def upgrade():
    op.create_table(
        "household_calendar_selections",
        identifier(),
        ref("connection_id", "calendar_connections.id", index=True),
        sa.Column("external_calendar_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("access_role", sa.String(32)),
        sa.Column("include_in_conflicts", sa.Boolean(), nullable=False),
        sa.Column("can_write", sa.Boolean(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("connection_id", "external_calendar_id", name="uq_household_calendar"),
    )
    # Read legacy metadata once. Never move/delete tutor records or credentials.
    op.execute(
        sa.text("""
        INSERT INTO household_calendar_selections
            (id, connection_id, external_calendar_id, name, access_role,
             include_in_conflicts, can_write)
        SELECT gen_random_uuid(), sc.connection_id, sc.external_calendar_id,
               min(sc.name), min(sc.access_role), bool_or(sc.include_in_conflicts),
               bool_or(sc.can_write)
        FROM scheduling_calendars sc
        JOIN calendar_connections cc ON cc.id = sc.connection_id
        WHERE cc.household_id IS NOT NULL
        GROUP BY sc.connection_id, sc.external_calendar_id
    """)
    )
    op.create_table(
        "assistant_conversations",
        identifier(),
        ref("user_id", "users.id", index=True),
        ref("household_id", "households.id"),
        sa.Column("channel_key", sa.String(150), nullable=False),
        sa.Column("history", postgresql.JSONB(), nullable=False),
        sa.Column("pending", postgresql.JSONB()),
        *timestamps(),
        sa.UniqueConstraint("user_id", "channel_key", name="uq_assistant_conversation"),
    )
    op.create_table(
        "assistant_turns",
        identifier(),
        ref("conversation_id", "assistant_conversations.id", index=True),
        sa.Column("message_key", sa.String(150), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("response", sa.Text()),
        *timestamps(),
        sa.UniqueConstraint("conversation_id", "message_key", name="uq_assistant_turn"),
    )
    op.create_table(
        "assistant_actions",
        identifier(),
        ref("turn_id", "assistant_turns.id"),
        sa.Column("action_key", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("turn_id", "action_key", name="uq_assistant_action"),
    )
    op.create_table(
        "assistant_budgets",
        identifier(),
        ref("household_id", "households.id"),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("household_id", "month", name="uq_assistant_budget"),
    )
    op.create_table(
        "assistant_model_calls",
        identifier(),
        ref("household_id", "households.id"),
        ref("turn_id", "assistant_turns.id"),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("route", sa.String(30), nullable=False),
        sa.Column("prompt_version", sa.String(30), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        *timestamps(),
    )


def downgrade():
    for table in (
        "assistant_model_calls",
        "assistant_budgets",
        "assistant_actions",
        "assistant_turns",
        "assistant_conversations",
        "household_calendar_selections",
    ):
        op.drop_table(table)
