"""Tool arguments are validated independently of model-provided confidence."""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

ShortText = Annotated[str, Field(min_length=1, max_length=255)]
LocalTime = Annotated[
    time,
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9])?$",
            "description": "Local wall-clock time HH:MM, without a timezone offset.",
        }
    ),
]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WriteArguments(Arguments):
    request_complete: bool = Field(
        default=False,
        description="True only if this tool fulfills the ENTIRE user request and needs no further "
        "reads, actions or explanation. The app returns its receipt without another model call.",
    )


class ConversationAnswer(Arguments):
    reply: str = Field(min_length=1, max_length=8000)
    topic: Literal["shopping", "tasks", "finance", "planning", "calendar", "general"]
    needs_clarification: bool


def answer_format():
    return {
        "type": "json_schema",
        "name": "household_answer",
        "strict": True,
        "schema": ConversationAnswer.model_json_schema(),
    }


class ListRecords(Arguments):
    kind: Literal["shopping", "tasks"]
    query: str | None = Field(default=None, max_length=100)
    offset: int = Field(default=0, ge=0, le=10000)


class NewRecord(Arguments):
    kind: Literal["shopping", "task"]
    title: ShortText
    store: str | None = Field(default=None, max_length=255)
    due_date: date | None = None

    @model_validator(mode="after")
    def applicable_fields(self):
        if self.kind == "task" and self.store is not None:
            raise ValueError("Tasks do not have stores")
        if self.kind == "shopping" and self.due_date is not None:
            raise ValueError("Shopping items do not have due dates")
        return self


class CreateRecords(WriteArguments):
    items: list[NewRecord] = Field(min_length=1, max_length=20)


class ChangeRecord(WriteArguments):
    kind: Literal["shopping", "task"]
    record_id: UUID
    action: Literal["rename", "set_store", "reschedule", "complete", "remove"]
    value: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def valid_change(self):
        if self.action in {"rename", "reschedule"} and not self.value:
            raise ValueError("A new title or date is required")
        if self.action == "reschedule":
            date.fromisoformat(self.value)
            if self.kind != "task":
                raise ValueError("Only tasks can be rescheduled")
        if self.action == "set_store" and self.kind != "shopping":
            raise ValueError("Only shopping items have a store")
        if self.action in {"remove", "complete"} and self.value is not None:
            raise ValueError("This action takes no value")
        return self


class FinanceQuery(Arguments):
    start_date: date
    end_date: date
    source: Literal["transactions", "receipts"] = "transactions"
    kind: Literal["expense", "income"] = "expense"
    category: str | None = Field(default=None, max_length=100)
    search: str | None = Field(default=None, max_length=100)
    offset: int = Field(default=0, ge=0, le=10000)

    @model_validator(mode="after")
    def valid_range(self):
        if not 0 <= (self.end_date - self.start_date).days <= 3660:
            raise ValueError("Choose an ordered date range of at most ten years")
        if self.source == "receipts" and (self.kind == "income" or self.category):
            raise ValueError("Receipts support expense totals and item/merchant search")
        return self


class RecordTransaction(WriteArguments):
    description: ShortText
    amount: Annotated[
        Decimal,
        Field(gt=0, le=100000000, max_digits=12, decimal_places=2),
        # Pydantic's generated Decimal regex uses lookaround, which the provider rejects.
        # Send exact decimal strings; the independent runtime bounds still apply.
        WithJsonSchema(
            {
                "type": "string",
                "pattern": r"^[0-9]{1,9}(\.[0-9]{1,2})?$",
                "description": "Positive decimal, at most 100000000, up to two decimals.",
            }
        ),
    ]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    kind: Literal["expense", "income"]
    category: ShortText
    occurred_on: date


class DayPlan(Arguments):
    day: date


class SavePlanning(WriteArguments):
    day: date
    work_start: LocalTime | None = None
    work_end: LocalTime | None = None
    note: str | None = Field(default=None, max_length=1000)
    note_mode: Literal["append", "replace"] = "append"

    @model_validator(mode="after")
    def valid_plan(self):
        if any(t is not None and t.tzinfo is not None for t in (self.work_start, self.work_end)):
            raise ValueError("Use local work times without timezone offsets")
        if self.work_start is None and self.work_end is None and not self.note:
            raise ValueError("Provide a work time or planning note")
        if self.work_start and self.work_end and self.work_start >= self.work_end:
            raise ValueError("Work end must follow work start")
        return self


class SaveWorkSchedule(WriteArguments):
    start_date: date
    end_date: date
    weekdays: list[int] = Field(
        min_length=1,
        max_length=7,
        description="Selected ISO weekdays: Monday=1 through Sunday=7. Ask if unclear.",
    )
    work_start: LocalTime
    work_end: LocalTime

    @model_validator(mode="after")
    def valid_schedule(self):
        if not 0 <= (self.end_date - self.start_date).days <= 365:
            raise ValueError("Choose an ordered date range of at most one year")
        if len(set(self.weekdays)) != len(self.weekdays) or any(
            d not in range(1, 8) for d in self.weekdays
        ):
            raise ValueError("Choose distinct ISO weekdays from 1 to 7")
        if self.work_start.tzinfo or self.work_end.tzinfo or self.work_start >= self.work_end:
            raise ValueError("Use local work times with the end after the start")
        return self


class PurchaseHistory(Arguments):
    start_date: date
    end_date: date
    limit: int = Field(default=15, ge=1, le=30)

    @model_validator(mode="after")
    def valid_range(self):
        if not 0 <= (self.end_date - self.start_date).days <= 3660:
            raise ValueError("Choose an ordered date range of at most ten years")
        return self


class PendingAction(Arguments):
    format: Literal["code", "details"] = "code"


class CalendarChange(Arguments):
    action: Literal["create", "update", "delete"]
    record_id: UUID | None = None
    title: ShortText | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @model_validator(mode="after")
    def valid_event(self):
        if self.action != "create" and self.record_id is None:
            raise ValueError("Select an existing event first")
        if self.action == "create" and (not self.title or not self.starts_at):
            raise ValueError("Creating an event requires title, start and end")
        if (self.starts_at is None) != (self.ends_at is None):
            raise ValueError("Both start and end are required")
        if self.starts_at is not None:
            if self.starts_at.utcoffset() is None or self.ends_at.utcoffset() is None:
                raise ValueError("Event timestamps require timezone offsets")
            if self.ends_at <= self.starts_at:
                raise ValueError("Event end must follow start")
        if self.action == "update" and not self.title and not self.starts_at:
            raise ValueError("Provide a changed title or time")
        if self.action == "delete" and (self.title or self.starts_at or self.ends_at):
            raise ValueError("Deletion only accepts the selected event")
        return self


class Escalate(Arguments):
    reason: Literal["complex_planning", "unresolved_reference", "conflicting_constraints"]


TOOL_MODELS = {
    "pending_action": (
        PendingAction,
        "Show the active confirmation or its exact copyable reply. "
        "Use only for help about the current pending change; does not confirm it.",
    ),
    "save_work_schedule": (
        SaveWorkSchedule,
        "Save work hours for a date range and selected weekdays "
        "in ONE atomic operation. Use for a whole month or recurring workdays "
        "within explicit dates instead of calling save_planning for each day.",
    ),
    "purchase_history": (
        PurchaseHistory,
        "Read purchased receipt items ranked by purchase frequency, "
        "last purchase, typical gap and whether already on the shopping list. "
        "Use for what we bought before, often buy, or should consider buying again. "
        "Read-only; never adds suggestions automatically.",
    ),
    "list_records": (
        ListRecords,
        "Find current shopping items or your tasks; use returned IDs for edits.",
    ),
    "create_records": (CreateRecords, "Add explicitly requested shopping items/tasks atomically."),
    "change_record": (
        ChangeRecord,
        "Edit one previously retrieved record. Removal needs confirmation.",
    ),
    "finance_query": (
        FinanceQuery,
        (
            "Exact totals by currency and paginated details. Sources are "
            "separate; never add receipts to bank totals. Dates use recorded "
            "transaction dates."
        ),
    ),
    "record_transaction": (
        RecordTransaction,
        (
            "Record an explicitly stated expense/income; never transfer "
            "money. Ask for missing amount/currency/date."
        ),
    ),
    "day_plan": (
        DayPlan,
        "Read tasks, routines, planning answers and cached calendar events for a day.",
    ),
    "save_planning": (
        SavePlanning,
        (
            "Save explicitly stated work times or planning context; a "
            "topic change is not a planning answer."
        ),
    ),
    "calendar_change": (
        CalendarChange,
        (
            "Prepare a household calendar change for explicit confirmation. "
            "Never book lessons or contact attendees."
        ),
    ),
    "escalate": (
        Escalate,
        (
            "Request the reasoning model for a difficult request. Missing "
            "user facts require a question instead."
        ),
    ),
}


def tool_definitions():
    def strict_schema(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                strict_schema(value)
        elif isinstance(node, list):
            for value in node:
                strict_schema(value)

    tools = []
    for name, (model, description) in TOOL_MODELS.items():
        schema = model.model_json_schema()
        strict_schema(schema)
        tools.append(
            {
                "type": "function",
                "name": name,
                "description": description,
                "parameters": schema,
                "strict": True,
            }
        )
    return tools
