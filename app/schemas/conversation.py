"""Tool arguments are validated independently of model-provided confidence."""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

ShortText = Annotated[str, Field(min_length=1, max_length=255)]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


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


class CreateRecords(Arguments):
    items: list[NewRecord] = Field(min_length=1, max_length=20)


class ChangeRecord(Arguments):
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


class RecordTransaction(Arguments):
    description: ShortText
    amount: Annotated[
        Decimal,
        Field(gt=0, le=100000000, max_digits=12, decimal_places=2),
        # Pydantic's generated Decimal regex uses lookaround, which the provider rejects.
        # Send exact decimal strings; the independent runtime bounds still apply.
        WithJsonSchema({
            "type": "string",
            "pattern": r"^[0-9]{1,9}(\.[0-9]{1,2})?$",
            "description": "Positive decimal amount, at most 100000000, with up to two decimals.",
        }),
    ]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    kind: Literal["expense", "income"]
    category: ShortText
    occurred_on: date


class DayPlan(Arguments):
    day: date


class SavePlanning(Arguments):
    day: date
    work_start: time | None = None
    work_end: time | None = None
    note: str | None = Field(default=None, max_length=1000)
    note_mode: Literal["append", "replace"] = "append"

    @model_validator(mode="after")
    def valid_plan(self):
        if self.work_start is None and self.work_end is None and not self.note:
            raise ValueError("Provide a work time or planning note")
        if self.work_start and self.work_end and self.work_start >= self.work_end:
            raise ValueError("Work end must follow work start")
        return self


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
