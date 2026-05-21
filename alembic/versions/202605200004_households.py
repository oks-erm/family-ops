"""households

Revision ID: 202605200004
Revises: 202605200003
Create Date: 2026-05-20

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "202605200004"
down_revision: str | None = "202605200003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    household_role = postgresql.ENUM("owner", "member", name="householdrole")
    household_role.create(op.get_bind(), checkfirst=True)
    household_role_column = postgresql.ENUM(
        "owner", "member", name="householdrole", create_type=False
    )

    op.create_table(
        "households",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("invite_code", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invite_code", name="uq_households_invite_code"),
    )
    op.create_index(op.f("ix_households_invite_code"), "households", ["invite_code"], unique=True)

    op.create_table(
        "household_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", household_role_column, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_household_members_user_id"),
    )
    op.create_index(op.f("ix_household_members_household_id"), "household_members", ["household_id"], unique=False)
    op.create_index(op.f("ix_household_members_user_id"), "household_members", ["user_id"], unique=False)

    op.add_column("shopping_items", sa.Column("household_id", sa.Uuid(), nullable=True))
    op.add_column("receipts", sa.Column("household_id", sa.Uuid(), nullable=True))
    op.add_column("pending_receipts", sa.Column("household_id", sa.Uuid(), nullable=True))

    op.create_foreign_key(
        "fk_shopping_items_household_id_households",
        "shopping_items",
        "households",
        ["household_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_receipts_household_id_households",
        "receipts",
        "households",
        ["household_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_pending_receipts_household_id_households",
        "pending_receipts",
        "households",
        ["household_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_shopping_items_household_id"), "shopping_items", ["household_id"], unique=False)
    op.create_index(op.f("ix_receipts_household_id"), "receipts", ["household_id"], unique=False)
    op.create_index(op.f("ix_pending_receipts_household_id"), "pending_receipts", ["household_id"], unique=False)

    op.execute("create temporary table user_household_backfill (user_id uuid, household_id uuid)")
    op.execute(
        """
        insert into user_household_backfill (user_id, household_id)
        select id, gen_random_uuid()
        from users
        """
    )
    op.execute(
        """
        insert into households (id, name, invite_code, created_at, updated_at)
        select b.household_id, coalesce(u.first_name, 'Household') || '''s household',
               upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 8)), now(), now()
        from user_household_backfill b
        join users u on u.id = b.user_id
        """
    )
    op.execute(
        """
        insert into household_members (id, household_id, user_id, role, created_at, updated_at)
        select gen_random_uuid(), b.household_id, b.user_id, 'owner', now(), now()
        from user_household_backfill b
        """
    )
    op.execute(
        """
        update shopping_items si
        set household_id = hm.household_id
        from household_members hm
        where si.user_id = hm.user_id and si.household_id is null
        """
    )
    op.execute(
        """
        update receipts r
        set household_id = hm.household_id
        from household_members hm
        where r.user_id = hm.user_id and r.household_id is null
        """
    )
    op.execute(
        """
        update pending_receipts pr
        set household_id = hm.household_id
        from household_members hm
        where pr.user_id = hm.user_id and pr.household_id is null
        """
    )

    op.alter_column("shopping_items", "household_id", nullable=False)
    op.alter_column("receipts", "household_id", nullable=False)
    op.alter_column("pending_receipts", "household_id", nullable=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_pending_receipts_household_id"), table_name="pending_receipts")
    op.drop_index(op.f("ix_receipts_household_id"), table_name="receipts")
    op.drop_index(op.f("ix_shopping_items_household_id"), table_name="shopping_items")
    op.drop_constraint("fk_pending_receipts_household_id_households", "pending_receipts", type_="foreignkey")
    op.drop_constraint("fk_receipts_household_id_households", "receipts", type_="foreignkey")
    op.drop_constraint("fk_shopping_items_household_id_households", "shopping_items", type_="foreignkey")
    op.drop_column("pending_receipts", "household_id")
    op.drop_column("receipts", "household_id")
    op.drop_column("shopping_items", "household_id")
    op.drop_index(op.f("ix_household_members_user_id"), table_name="household_members")
    op.drop_index(op.f("ix_household_members_household_id"), table_name="household_members")
    op.drop_table("household_members")
    op.drop_index(op.f("ix_households_invite_code"), table_name="households")
    op.drop_table("households")
    postgresql.ENUM(name="householdrole").drop(op.get_bind(), checkfirst=True)
