"""Scoped household operations. These methods flush but leave commits to the action ledger."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Date, Numeric, and_, case, cast, func, or_, select

from app.db.models import (
    ActivityAction,
    ActivityLog,
    CalendarEventCache,
    FinancialTransaction,
    PlanningConversation,
    PlanningConversationState,
    Receipt,
    ReceiptItem,
    Routine,
    ShoppingItem,
    ShoppingItemStatus,
    Task,
    TaskCompletion,
    TaskStatus,
    TransactionType,
)


class RecordNotFound(ValueError):
    pass


class AssistantDataRepository:
    def __init__(self, session, *, user_id: UUID, household_id: UUID, timezone: str):
        self.session = session
        self.user_id = user_id
        self.household_id = household_id
        self.timezone = timezone

    def _scope(self, kind):
        if kind == "shopping":
            return ShoppingItem, [ShoppingItem.household_id == self.household_id]
        return Task, [Task.household_id == self.household_id, Task.user_id == self.user_id]

    @staticmethod
    def record(item):
        if isinstance(item, ShoppingItem):
            return {
                "id": str(item.id),
                "kind": "shopping",
                "title": item.name,
                "store": item.store_name_raw,
                "status": item.status.value,
            }
        return {
            "id": str(item.id),
            "kind": "task",
            "title": item.title,
            "due_date": item.due_date.isoformat() if item.due_date else None,
            "status": item.status.value,
        }

    async def list_records(self, args):
        cls, filters = self._scope(args.kind)
        filters.append(
            cls.status
            == (ShoppingItemStatus.pending if cls is ShoppingItem else TaskStatus.pending)
        )
        if args.query:
            column = cls.name if cls is ShoppingItem else cls.title
            filters.append(column.icontains(args.query, autoescape=True))
        total = await self.session.scalar(select(func.count()).select_from(cls).where(*filters))
        items = (
            await self.session.scalars(
                select(cls)
                .where(*filters)
                .order_by(cls.created_at, cls.id)
                .offset(args.offset)
                .limit(30)
            )
        ).all()
        return {
            "records": [self.record(item) for item in items],
            "total": total,
            "next_offset": args.offset + 30 if total > args.offset + 30 else None,
        }

    async def get_record(self, kind, record_id, *, lock=False):
        cls, filters = self._scope(kind)
        query = select(cls).where(cls.id == record_id, *filters)
        if lock:
            query = query.with_for_update()
        record = await self.session.scalar(query)
        if record is None:
            raise RecordNotFound("That record is no longer available. Please select it again.")
        return record

    async def log(self, record, action, entity_type, summary):
        self.session.add(
            ActivityLog(
                household_id=self.household_id,
                user_id=self.user_id,
                action=action,
                entity_type=entity_type,
                entity_id=record.id,
                summary=summary[:500],
                metadata_json={"source": "assistant_v2"},
            )
        )

    async def create_records(self, args):
        records = []
        for item in args.items:
            if item.kind == "shopping":
                record = ShoppingItem(
                    user_id=self.user_id,
                    household_id=self.household_id,
                    name=item.title,
                    store_name_raw=item.store,
                    status=ShoppingItemStatus.pending,
                )
            else:
                record = Task(
                    user_id=self.user_id,
                    household_id=self.household_id,
                    title=item.title,
                    due_date=item.due_date,
                    status=TaskStatus.pending,
                )
            self.session.add(record)
            await self.session.flush()
            await self.log(record, ActivityAction.created, item.kind, f"Added {item.title}")
            records.append(self.record(record))
        return {"records": records, "message": "Added: " + "; ".join(r["title"] for r in records)}

    async def change_record(self, args):
        record = await self.get_record(args.kind, args.record_id, lock=True)
        if record.status.value != "pending":
            raise ValueError("That item is no longer pending. Please review its current state.")
        if args.action == "rename":
            if args.kind == "shopping":
                record.name = args.value
            else:
                record.title = args.value
        elif args.action == "set_store":
            record.store_name_raw = args.value
        elif args.action == "reschedule":
            record.due_date = date.fromisoformat(args.value)
            record.moved_count += 1
        elif args.action == "complete":
            if args.kind == "shopping":
                record.status = ShoppingItemStatus.purchased
            else:
                record.status = TaskStatus.done
                self.session.add(
                    TaskCompletion(
                        task_id=record.id,
                        user_id=self.user_id,
                        household_id=self.household_id,
                        completed_on=datetime.now(ZoneInfo(self.timezone)).date(),
                        status=TaskStatus.done,
                    )
                )
        elif args.action == "remove":
            # Retain history; the public pending list excludes skipped records.
            record.status = (
                ShoppingItemStatus.skipped if args.kind == "shopping" else TaskStatus.skipped
            )
        await self.session.flush()
        result = self.record(record)
        await self.log(
            record, ActivityAction.updated, args.kind, f"{args.action}: {result['title']}"
        )
        return {"record": result, "message": f"Updated {result['title']}: {args.action}."}

    async def record_transaction(self, args):
        record = FinancialTransaction(
            user_id=self.user_id,
            household_id=self.household_id,
            description=args.description,
            amount=str(args.amount),
            currency=args.currency,
            category=args.category,
            transaction_type=TransactionType(args.kind),
            occurred_on=args.occurred_on,
            source="manual",
            raw_data={},
        )
        self.session.add(record)
        await self.session.flush()
        await self.log(
            record,
            ActivityAction.created,
            "financial_transaction",
            f"Recorded {args.kind}: {args.description}",
        )
        return {
            "id": str(record.id),
            "message": (
                f"Recorded {args.kind}: {args.description}, {args.amount} {args.currency} "
                f"on {args.occurred_on}."
            ),
        }

    async def calendar_record(self, record_id):
        record = await self.session.scalar(
            select(CalendarEventCache).where(
                CalendarEventCache.id == record_id,
                CalendarEventCache.household_id == self.household_id,
            )
        )
        if record is None:
            raise RecordNotFound("That event is no longer available. Please select it again.")
        return record

    async def day_plan(self, args):
        start = datetime.combine(args.day, time.min, tzinfo=ZoneInfo(self.timezone))
        end = start + timedelta(days=1)
        events = (
            await self.session.scalars(
                select(CalendarEventCache)
                .where(
                    CalendarEventCache.household_id == self.household_id,
                    CalendarEventCache.starts_at < end,
                    CalendarEventCache.ends_at > start,
                )
                .order_by(CalendarEventCache.starts_at)
                .limit(101)
            )
        ).all()
        tasks = (
            await self.session.scalars(
                select(Task)
                .where(
                    Task.household_id == self.household_id,
                    Task.user_id == self.user_id,
                    Task.status == TaskStatus.pending,
                    or_(Task.due_date <= args.day, Task.due_date.is_(None)),
                )
                .order_by(Task.due_date.nulls_last(), Task.created_at)
                .limit(101)
            )
        ).all()
        planning = await self.session.scalar(
            select(PlanningConversation).where(
                PlanningConversation.user_id == self.user_id,
                PlanningConversation.household_id == self.household_id,
                PlanningConversation.plan_date == args.day,
            )
        )
        routines = (
            await self.session.scalars(
                select(Routine)
                .where(
                    Routine.household_id == self.household_id,
                    Routine.is_active.is_(True),
                )
                .limit(50)
            )
        ).all()
        return {
            "day": str(args.day),
            "timezone": self.timezone,
            "calendar_source": "synced cache; not a live availability guarantee",
            "events": [
                {
                    "id": str(e.id),
                    "title": e.title,
                    "start": e.starts_at.astimezone(ZoneInfo(self.timezone)).isoformat(),
                    "end": e.ends_at.astimezone(ZoneInfo(self.timezone)).isoformat(),
                    "synced_at": e.updated_at.isoformat(),
                }
                for e in events[:100]
            ],
            "tasks": [self.record(t) for t in tasks[:100]],
            "truncated": len(events) > 100 or len(tasks) > 100,
            "routines": [{"title": r.title, "schedule": r.schedule} for r in routines],
            "planning": None
            if planning is None
            else {
                "work_start": str(planning.work_start) if planning.work_start else None,
                "work_end": str(planning.work_end) if planning.work_end else None,
                "notes": planning.unusual_notes,
                "state": planning.state.value,
            },
        }

    async def save_planning(self, args):
        planning = await self.session.scalar(
            select(PlanningConversation)
            .where(
                PlanningConversation.user_id == self.user_id,
                PlanningConversation.plan_date == args.day,
            )
            .with_for_update()
        )
        if planning is None:
            planning = PlanningConversation(
                user_id=self.user_id,
                household_id=self.household_id,
                plan_date=args.day,
                state=PlanningConversationState.awaiting_work_start,
                raw_notes=[],
            )
            self.session.add(planning)
        start = args.work_start or planning.work_start
        end = args.work_end or planning.work_end
        if start and end and start >= end:
            raise ValueError("Work end must follow work start")
        planning.work_start, planning.work_end = start, end
        if args.note is not None:
            if args.note_mode == "replace" or not planning.unusual_notes:
                planning.unusual_notes = args.note
            elif args.note not in planning.unusual_notes.split("; "):
                planning.unusual_notes = f"{planning.unusual_notes}; {args.note}"
        if not start:
            planning.state = PlanningConversationState.awaiting_work_start
        elif not end:
            planning.state = PlanningConversationState.awaiting_work_end
        else:
            planning.state = PlanningConversationState.complete
        await self.session.flush()
        await self.log(
            planning, ActivityAction.updated, "planning", f"Updated planning for {args.day}"
        )
        return {"message": f"Saved planning for {args.day}.", "state": planning.state.value}

    async def active_planning(self):
        now = datetime.now(UTC)
        planning = await self.session.scalar(
            select(PlanningConversation)
            .where(
                PlanningConversation.user_id == self.user_id,
                PlanningConversation.household_id == self.household_id,
                PlanningConversation.plan_date > now.astimezone(ZoneInfo(self.timezone)).date(),
                PlanningConversation.updated_at > now - timedelta(hours=6),
                PlanningConversation.state != PlanningConversationState.complete,
            )
            .order_by(PlanningConversation.updated_at.desc())
            .limit(1)
        )
        return (
            None
            if planning is None
            else {"day": str(planning.plan_date), "state": planning.state.value}
        )

    async def finance_query(self, args):
        if args.source == "transactions":
            cls = FinancialTransaction
            # Preserve the dashboard's existing effective-date semantics explicitly.
            effective_date = case(
                (
                    and_(
                        func.extract("year", cls.occurred_on)
                        == func.extract("year", cls.created_at),
                        func.extract("month", cls.occurred_on)
                        == func.extract("month", cls.created_at),
                    ),
                    cls.occurred_on,
                ),
                else_=cast(cls.created_at, Date),
            )
            query = select(
                cls.id,
                effective_date.label("day"),
                cls.description.label("label"),
                cls.amount.label("amount"),
                cls.currency.label("currency"),
            ).where(
                cls.household_id == self.household_id,
                cls.transaction_type == TransactionType(args.kind),
                effective_date >= args.start_date,
                effective_date <= args.end_date,
            )
            if args.category:
                categories = {
                    "commute": ["Uber", "Gas", "Tolls", "Public Transport"],
                    "transport": ["Uber", "Gas", "Tolls", "Public Transport"],
                }
                matches = categories.get(args.category.casefold(), [args.category])
                query = query.where(func.lower(cls.category).in_([c.casefold() for c in matches]))
            if args.search:
                query = query.where(
                    or_(
                        cls.description.icontains(args.search, autoescape=True),
                        cls.merchant.icontains(args.search, autoescape=True),
                    )
                )
            basis = "dashboard effective date; dates outside the import month use the import date"
        else:
            effective_date = func.coalesce(Receipt.purchased_at, cast(Receipt.created_at, Date))
            if args.search:
                query = (
                    select(
                        ReceiptItem.id,
                        effective_date.label("day"),
                        ReceiptItem.name.label("label"),
                        ReceiptItem.total_amount.label("amount"),
                        Receipt.currency.label("currency"),
                    )
                    .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
                    .where(ReceiptItem.name.icontains(args.search, autoescape=True))
                )
            else:
                query = select(
                    Receipt.id,
                    effective_date.label("day"),
                    Receipt.shop_name.label("label"),
                    Receipt.total_amount.label("amount"),
                    Receipt.currency.label("currency"),
                )
            query = query.where(
                Receipt.household_id == self.household_id,
                effective_date >= args.start_date,
                effective_date <= args.end_date,
            )
            basis = "receipt purchase date (import date when missing)"
        rows = query.subquery()
        cleaned = func.trim(func.replace(func.replace(rows.c.amount, ",", "."), "€", ""))
        valid = cleaned.op("~")(r"^[+-]?[0-9]+([.][0-9]+)?$")
        amount = case((valid, cast(cleaned, Numeric())), else_=None)
        totals = (
            await self.session.execute(
                select(
                    rows.c.currency,
                    func.sum(amount),
                    func.count(),
                    func.count(amount),
                ).group_by(rows.c.currency)
            )
        ).all()
        details = (
            (
                await self.session.execute(
                    select(rows).order_by(rows.c.day, rows.c.id).offset(args.offset).limit(30)
                )
            )
            .mappings()
            .all()
        )
        count = sum(row[2] for row in totals)
        invalid = sum(row[2] - row[3] for row in totals)
        return {
            "source": args.source,
            "date_basis": basis,
            "start_date": str(args.start_date),
            "end_date": str(args.end_date),
            "totals": [
                {"currency": r[0] or "unknown", "amount": str(r[1] or Decimal("0")), "count": r[2]}
                for r in totals
            ],
            "invalid_amounts": invalid,
            "complete": invalid == 0,
            "count": count,
            "details": [
                {**dict(row), "id": str(row["id"]), "day": str(row["day"])} for row in details
            ],
            "next_offset": args.offset + 30 if count > args.offset + 30 else None,
        }
