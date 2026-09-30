"""Durable household inbox/outbox, short leases, context and optional dollar budgets."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "202609300002"
down_revision = "202609300001"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column(n, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
        for n in ("created_at", "updated_at")
    ]


def upgrade():
    # Preserve same-day plans when membership changes. Keep constraint names for old writers.
    for table, name in (
        ("daily_plans", "uq_daily_plans_user_date"),
        ("planning_conversations", "uq_planning_conversations_user_date"),
    ):
        op.drop_constraint(name, table, type_="unique")
        op.create_unique_constraint(name, table, ["user_id", "household_id", "plan_date"])
    op.add_column(
        "assistant_conversations",
        sa.Column("context", JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "assistant_budgets",
        sa.Column("estimated_usd", sa.Numeric(16, 8), nullable=False, server_default="0"),
    )
    op.add_column("assistant_model_calls", sa.Column("estimated_usd", sa.Numeric(16, 8)))
    op.add_column(
        "assistant_model_calls",
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    # Conservatively account for earlier calls before allowing a mid-month dollar cap.
    # Old rows did not track cache writes; treat every uncached token as a cache write.
    op.execute(
        sa.text("""
        UPDATE assistant_model_calls SET estimated_usd =
          CASE model WHEN 'gpt-6-luna' THEN 0.10 WHEN 'gpt-6.1-sol' THEN 2.0
                     WHEN 'gpt-6-astra' THEN 10.0 END *
          CASE WHEN status IN ('completed', 'incomplete')
            THEN ((input_tokens - cached_tokens) * 1.25 + cached_tokens * 0.10 + output_tokens * 5)
            ELSE reserved_tokens * 6.25 END / 1000000,
          cache_write_tokens = greatest(0, input_tokens - cached_tokens)
    """)
    )
    op.execute(
        sa.text("""
        UPDATE assistant_budgets b SET estimated_usd = coalesce((
          SELECT sum(c.estimated_usd) FROM assistant_model_calls c
          WHERE c.household_id = b.household_id
            AND date_trunc('month', c.created_at AT TIME ZONE 'UTC')::date = b.month
        ), 0)
    """)
    )
    op.create_table(
        "runtime_leases",
        sa.Column("name", sa.String(180), primary_key=True),
        sa.Column("owner", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.create_table(
        "transport_cursors",
        sa.Column("name", sa.String(80), primary_key=True),
        sa.Column("offset", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "assistant_household_policies",
        sa.Column(
            "household_id",
            sa.Uuid(),
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("monthly_token_limit", sa.Integer()),
        sa.Column("monthly_usd_limit", sa.Numeric(16, 8)),
        sa.Column("max_concurrent", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("requests_per_minute", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "assistant_inbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("update_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("channel_key", sa.String(150), nullable=False),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("household_id", sa.Uuid(), sa.ForeignKey("households.id", ondelete="SET NULL")),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("replay_safe", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("owner", sa.Uuid()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(80)),
        *timestamps(),
    )
    op.create_index("ix_assistant_inbox_channel_order", "assistant_inbox", ["channel_key", "id"])
    op.create_index(
        "ix_assistant_inbox_household_status",
        "assistant_inbox",
        ["household_id", "status", "started_at"],
    )
    op.create_index("ix_assistant_inbox_ready", "assistant_inbox", ["status", "available_at", "id"])
    op.create_index(
        "ix_assistant_inbox_household_started", "assistant_inbox", ["household_id", "started_at"]
    )
    op.create_table(
        "assistant_outbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "inbox_id",
            sa.BigInteger(),
            sa.ForeignKey("assistant_inbox.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("part", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("owner", sa.Uuid()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("error_code", sa.String(80)),
        *timestamps(),
        sa.UniqueConstraint("inbox_id", "part", name="uq_assistant_delivery_part"),
    )
    op.create_index(
        "ix_assistant_outbox_ready", "assistant_outbox", ["status", "available_at", "id"]
    )
    op.create_index("ix_assistant_outbox_chat_order", "assistant_outbox", ["chat_id", "id"])


def downgrade():
    # Fail rather than discard records if different households now have same-day plans.
    for table, name in (
        ("daily_plans", "uq_daily_plans_user_date"),
        ("planning_conversations", "uq_planning_conversations_user_date"),
    ):
        op.drop_constraint(name, table, type_="unique")
        op.create_unique_constraint(name, table, ["user_id", "plan_date"])
    for table in (
        "assistant_outbox",
        "assistant_inbox",
        "assistant_household_policies",
        "transport_cursors",
        "runtime_leases",
    ):
        op.drop_table(table)
    op.drop_column("assistant_model_calls", "cache_write_tokens")
    op.drop_column("assistant_model_calls", "estimated_usd")
    op.drop_column("assistant_budgets", "estimated_usd")
    op.drop_column("assistant_conversations", "context")
