import enum
from datetime import date, datetime, time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Date, DateTime, Enum, ForeignKey, String, Text, Time, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TaskStatus(str, enum.Enum):
    pending = "pending"
    done = "done"
    skipped = "skipped"
    moved = "moved"


class ShoppingItemStatus(str, enum.Enum):
    pending = "pending"
    purchased = "purchased"
    skipped = "skipped"


class DailyPlanStatus(str, enum.Enum):
    draft = "draft"
    sent = "sent"
    reviewed = "reviewed"


class PlanningConversationState(str, enum.Enum):
    awaiting_work_start = "awaiting_work_start"
    awaiting_work_end = "awaiting_work_end"
    awaiting_unusual_notes = "awaiting_unusual_notes"
    complete = "complete"


class ReceiptStatus(str, enum.Enum):
    pending_confirmation = "pending_confirmation"
    extracted = "extracted"
    extraction_failed = "extraction_failed"


class HouseholdRole(str, enum.Enum):
    owner = "owner"
    member = "member"


class CalendarProvider(str, enum.Enum):
    google = "google"
    ical = "ical"


class TransactionType(str, enum.Enum):
    expense = "expense"
    income = "income"


class ActivityAction(str, enum.Enum):
    created = "created"
    updated = "updated"
    deleted = "deleted"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Lisbon")
    google_email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True)
    dashboard_link_token: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    dashboard_link_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tasks: Mapped[list["Task"]] = relationship(back_populates="user")
    shopping_items: Mapped[list["ShoppingItem"]] = relationship(back_populates="user")
    daily_plans: Mapped[list["DailyPlan"]] = relationship(back_populates="user")
    receipts: Mapped[list["Receipt"]] = relationship(back_populates="user")
    household_membership: Mapped["HouseholdMember | None"] = relationship(back_populates="user")


class Household(Base, TimestampMixin):
    __tablename__ = "households"
    __table_args__ = (UniqueConstraint("invite_code", name="uq_households_invite_code"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255))
    invite_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    members: Mapped[list["HouseholdMember"]] = relationship(back_populates="household")


class HouseholdMember(Base, TimestampMixin):
    __tablename__ = "household_members"
    __table_args__ = (UniqueConstraint("user_id", name="uq_household_members_user_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    household_id: Mapped[UUID] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[HouseholdRole] = mapped_column(Enum(HouseholdRole), default=HouseholdRole.member)

    household: Mapped[Household] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="household_membership")


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    household_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(500))
    category: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.pending)
    due_date: Mapped[date | None] = mapped_column(Date)
    moved_count: Mapped[int] = mapped_column(default=0)

    user: Mapped[User] = relationship(back_populates="tasks")


class TaskCompletion(Base, TimestampMixin):
    __tablename__ = "task_completions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    household_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    completed_on: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus))


class Store(Base, TimestampMixin):
    __tablename__ = "stores"
    __table_args__ = (UniqueConstraint("name", name="uq_stores_name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255))


class ShoppingItem(Base, TimestampMixin):
    __tablename__ = "shopping_items"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    household_id: Mapped[UUID] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[UUID | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(255))
    store_name_raw: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[ShoppingItemStatus] = mapped_column(
        Enum(ShoppingItemStatus), default=ShoppingItemStatus.pending
    )

    user: Mapped[User] = relationship(back_populates="shopping_items")
    store: Mapped[Store | None] = relationship()


class DailyPlan(Base, TimestampMixin):
    __tablename__ = "daily_plans"
    __table_args__ = (UniqueConstraint("user_id", "plan_date", name="uq_daily_plans_user_date"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    household_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    plan_date: Mapped[date] = mapped_column(Date)
    work_start: Mapped[time | None] = mapped_column(Time)
    work_end: Mapped[time | None] = mapped_column(Time)
    unusual_notes: Mapped[str | None] = mapped_column(Text)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[DailyPlanStatus] = mapped_column(Enum(DailyPlanStatus), default=DailyPlanStatus.draft)

    user: Mapped[User] = relationship(back_populates="daily_plans")


class PlanningConversation(Base, TimestampMixin):
    __tablename__ = "planning_conversations"
    __table_args__ = (UniqueConstraint("user_id", "plan_date", name="uq_planning_conversations_user_date"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    household_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    plan_date: Mapped[date] = mapped_column(Date)
    state: Mapped[PlanningConversationState] = mapped_column(Enum(PlanningConversationState))
    work_start: Mapped[time | None] = mapped_column(Time)
    work_end: Mapped[time | None] = mapped_column(Time)
    unusual_notes: Mapped[str | None] = mapped_column(Text)
    raw_notes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)


class Routine(Base, TimestampMixin):
    __tablename__ = "routines"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    household_id: Mapped[UUID] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    schedule: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(default=True)


class ExpenseCategory(Base, TimestampMixin):
    __tablename__ = "expense_categories"
    __table_args__ = (UniqueConstraint("name", name="uq_expense_categories_name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255))


class Receipt(Base, TimestampMixin):
    __tablename__ = "receipts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    household_id: Mapped[UUID] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    shop_name: Mapped[str | None] = mapped_column(String(255))
    purchased_at: Mapped[date | None] = mapped_column(Date)
    total_amount: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[ReceiptStatus] = mapped_column(Enum(ReceiptStatus), default=ReceiptStatus.extracted)
    raw_extraction: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    user: Mapped[User] = relationship(back_populates="receipts")
    items: Mapped[list["ReceiptItem"]] = relationship(back_populates="receipt")


class PendingReceipt(Base, TimestampMixin):
    __tablename__ = "pending_receipts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    household_id: Mapped[UUID] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    image_path: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(100))
    extraction: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ReceiptItem(Base, TimestampMixin):
    __tablename__ = "receipt_items"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    receipt_id: Mapped[UUID] = mapped_column(ForeignKey("receipts.id", ondelete="CASCADE"), index=True)
    category_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("expense_categories.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[str | None] = mapped_column(String(64))
    total_amount: Mapped[str | None] = mapped_column(String(64))

    receipt: Mapped[Receipt] = relationship(back_populates="items")
    category: Mapped[ExpenseCategory | None] = relationship()


class CalendarConnection(Base, TimestampMixin):
    __tablename__ = "calendar_connections"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    household_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[CalendarProvider] = mapped_column(Enum(CalendarProvider), default=CalendarProvider.google)
    external_account_id: Mapped[str | None] = mapped_column(String(255))
    access_token: Mapped[str | None] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list)


class ICalFeed(Base, TimestampMixin):
    __tablename__ = "ical_feeds"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    household_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)


class CalendarEventCache(Base, TimestampMixin):
    __tablename__ = "calendar_events_cache"
    __table_args__ = (UniqueConstraint("source_type", "source_id", "external_event_id", name="uq_calendar_event_source"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    household_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[CalendarProvider] = mapped_column(Enum(CalendarProvider))
    source_id: Mapped[UUID] = mapped_column(index=True)
    external_event_id: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(500))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    location: Mapped[str | None] = mapped_column(String(500))
    raw_event: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ScheduledJobLog(Base, TimestampMixin):
    __tablename__ = "scheduled_jobs_log"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_name: Mapped[str] = mapped_column(String(255), index=True)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    household_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("households.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(50))
    message: Mapped[str | None] = mapped_column(Text)


class HouseholdRecommendation(Base, TimestampMixin):
    __tablename__ = "household_recommendations"
    __table_args__ = (
        UniqueConstraint("household_id", "period_start", "period_end", name="uq_household_recommendation_period"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    household_id: Mapped[UUID] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    period_start: Mapped[date] = mapped_column(Date, index=True)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    recommendations: Mapped[list[str]] = mapped_column(JSONB, default=list)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class FinancialTransaction(Base, TimestampMixin):
    __tablename__ = "financial_transactions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    household_id: Mapped[UUID] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    transaction_type: Mapped[TransactionType] = mapped_column(Enum(TransactionType), index=True)
    category: Mapped[str] = mapped_column(String(100), index=True)
    merchant: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(String(500))
    amount: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(16), default="EUR")
    occurred_on: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(50), default="manual")
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ActivityLog(Base, TimestampMixin):
    __tablename__ = "activity_logs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    household_id: Mapped[UUID] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    action: Mapped[ActivityAction] = mapped_column(Enum(ActivityAction), index=True)
    entity_type: Mapped[str] = mapped_column(String(100), index=True)
    entity_id: Mapped[UUID | None] = mapped_column(index=True)
    summary: Mapped[str] = mapped_column(String(500))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ShoppingPriceQuote(Base, TimestampMixin):
    __tablename__ = "shopping_price_quotes"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    household_id: Mapped[UUID] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    shopping_item_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("shopping_items.id", ondelete="CASCADE"), index=True
    )
    item_name: Mapped[str] = mapped_column(String(255), index=True)
    store_name: Mapped[str] = mapped_column(String(255), index=True)
    product_name: Mapped[str | None] = mapped_column(String(500))
    price: Mapped[str | None] = mapped_column(String(64))
    old_price: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(16), default="EUR")
    product_url: Mapped[str | None] = mapped_column(Text)
    is_promotion: Mapped[bool] = mapped_column(default=False, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    source: Mapped[str] = mapped_column(String(50), default="web")
