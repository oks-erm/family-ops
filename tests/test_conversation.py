import json
import unittest
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.clients.conversation_model import ModelReply, ModelUnavailable, OpenAIConversationModel
from app.config import Settings
from app.db.repositories.conversations import BudgetExceeded
from app.schemas.conversation import (
    CalendarChange,
    ChangeRecord,
    CreateRecords,
    FinanceQuery,
    RecordTransaction,
    tool_definitions,
)
from app.services.conversation.routing import deterministic_request
from app.services.conversation.service import ConversationService


def settings(**values):
    return Settings(_env_file=None, **values)


def reply(text=None, tool=None, arguments=None):
    output = (
        [{"type": "message", "content": [{"type": "output_text", "text": text}]}]
        if text
        else [
            {
                "type": "function_call",
                "call_id": str(uuid4()),
                "name": tool,
                "arguments": json.dumps(arguments or {}),
            }
        ]
    )
    return ModelReply(output, {"input_tokens": 100, "output_tokens": 50}, "completed")


class ContractTests(unittest.TestCase):
    def test_exact_commands_bypass_models(self):
        self.assertEqual(deterministic_request("shopping list", date.today())[0], "list_records")
        self.assertEqual(
            deterministic_request("task: call dentist", date.today())[0], "create_records"
        )

    def test_variations_and_compound_requests_go_to_model(self):
        for text in [
            "what abot last month?",
            "need help",
            "task: call dentist tomorrow",
            "shopping list and tasks",
            "move that",
            "details of my tasks",
        ]:
            self.assertIsNone(deterministic_request(text, date.today()), text)

    def test_extra_fields_cannot_select_household(self):
        with self.assertRaises(ValidationError):
            CreateRecords.model_validate(
                {"items": [{"kind": "task", "title": "Hello"}], "household_id": str(uuid4())}
            )

    def test_bad_dates_and_wrong_domain_changes_rejected(self):
        for data in [
            dict(kind="shopping", action="reschedule", value="2026-10-01"),
            dict(kind="task", action="reschedule", value="Friday"),
            dict(kind="task", action="set_store", value="Lidl"),
        ]:
            with self.assertRaises(ValidationError):
                ChangeRecord(record_id=uuid4(), **data)

    def test_calendar_requires_timezone_and_end(self):
        with self.assertRaises(ValidationError):
            CalendarChange(
                action="create",
                title="Dentist",
                starts_at="2026-10-01T10:00:00",
                ends_at="2026-10-01T11:00:00",
            )

    def test_finance_date_range_and_source(self):
        for values in [
            dict(start_date="2026-10-01", end_date="2026-09-01"),
            dict(start_date="2026-10-01", end_date="2026-10-02", source="receipts", kind="income"),
        ]:
            with self.assertRaises(ValidationError):
                FinanceQuery(**values)

    def test_all_tool_objects_are_strict(self):
        def visit(value):
            if isinstance(value, dict):
                if value.get("type") == "object":
                    self.assertFalse(value["additionalProperties"])
                    self.assertEqual(set(value["required"]), set(value["properties"]))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(tool_definitions())

    def test_money_schema_preserves_exact_decimal_runtime_validation(self):
        schema = next(t for t in tool_definitions() if t["name"] == "record_transaction")
        amount = schema["parameters"]["properties"]["amount"]
        self.assertEqual(amount["type"], "string")
        self.assertNotIn("(?", amount["pattern"])
        values = dict(description="Synthetic", currency="EUR", kind="expense",
                      category="Other", occurred_on="2026-09-30")
        self.assertEqual(str(RecordTransaction(amount="12.34", **values).amount), "12.34")
        for invalid in ["0", "-1", "1.001", "100000000.01", "NaN", "Infinity"]:
            with self.subTest(amount=invalid), self.assertRaises(ValidationError):
                RecordTransaction(amount=invalid, **values)


class ModelClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_uses_responses_api_and_no_storage(self):
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "output": [],
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                    "status": "completed",
                },
            )

        client = OpenAIConversationModel(
            settings(OPENAI_API_KEY="test-key"), httpx.MockTransport(handler)
        )
        await client.respond(model="test", instructions="test", inputs=[], tools=[])
        self.assertFalse(seen[0]["store"])
        self.assertFalse(seen[0]["parallel_tool_calls"])

    async def test_error_is_redacted_and_never_automatically_retried(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(429, text="private provider payload")

        client = OpenAIConversationModel(
            settings(OPENAI_API_KEY="secret"), httpx.MockTransport(handler)
        )
        with self.assertRaises(ModelUnavailable) as caught:
            await client.respond(model="test", instructions="test", inputs=[], tools=[])
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(len(requests), 1)


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = SimpleNamespace(
            commit=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock()
        )
        self.repo = SimpleNamespace(
            reserve=AsyncMock(return_value=(SimpleNamespace(), date.today())),
            settle=AsyncMock(),
            action=AsyncMock(return_value=None),
        )
        self.model = SimpleNamespace(respond=AsyncMock())
        self.data = SimpleNamespace(
            session=self.session,
            timezone="Europe/Lisbon",
            household_id=uuid4(),
            active_planning=AsyncMock(
                return_value={"state": "awaiting_work_start", "day": "2026-10-01"}
            ),
            list_records=AsyncMock(return_value={"records": [], "total": 0, "next_offset": None}),
        )
        self.conversation = SimpleNamespace(history=[], pending=None)
        self.turn = SimpleNamespace(id=uuid4())
        self.service = ConversationService(
            self.session, settings(), model=self.model, repository=self.repo
        )

    async def test_exact_read_costs_zero_model_calls(self):
        text, _ = await self.service.run_turn(
            self.conversation, self.turn, self.data, "shopping list"
        )
        self.model.respond.assert_not_awaited()
        self.assertIn("0 pending", text)

    async def test_topic_switch_does_not_force_planning_answer(self):
        self.model.respond.side_effect = [
            reply(tool="list_records", arguments={"kind": "shopping"}),
            reply(text="Your shopping list is empty."),
        ]
        text, _ = await self.service.run_turn(
            self.conversation, self.turn, self.data, "What do I need from the shop?"
        )
        self.assertIn("shopping", text)
        self.data.list_records.assert_awaited_once()
        context = self.model.respond.call_args_list[0].kwargs["inputs"]
        self.assertTrue(any("awaiting_work_start" in str(item) for item in context))

    async def test_previous_turn_is_available_for_followup(self):
        self.conversation.history = [
            {"user": "How much for transport?", "assistant": "42 EUR", "evidence": []}
        ]
        self.model.respond.return_value = reply(text="Which period?")
        await self.service.run_turn(self.conversation, self.turn, self.data, "And last month?")
        self.assertIn(
            "How much for transport?", json.dumps(self.model.respond.call_args.kwargs["inputs"])
        )

    async def test_model_can_escalate_once(self):
        self.model.respond.side_effect = [
            reply(tool="escalate", arguments={"reason": "conflicting_constraints"}),
            reply(text="A proposed plan."),
        ]
        await self.service.run_turn(
            self.conversation, self.turn, self.data, "Help balance the week's constraints"
        )
        self.assertEqual(
            [c.kwargs["model"] for c in self.model.respond.call_args_list],
            ["gpt-6-luna", "gpt-6.1-sol"],
        )

    async def test_budget_exhaustion_never_calls_provider(self):
        self.repo.reserve.side_effect = BudgetExceeded
        text, _ = await self.service.run_turn(
            self.conversation, self.turn, self.data, "Could you help?"
        )
        self.assertIn("allowance", text)
        self.model.respond.assert_not_awaited()

    async def test_new_topic_retires_pending_confirmation(self):
        self.conversation.pending = {
            "token": "abcd",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }
        await self.service.run_turn(self.conversation, self.turn, self.data, "shopping list")
        self.assertIsNone(self.conversation.pending)

    async def test_expired_confirmation_does_not_execute(self):
        self.conversation.pending = {
            "token": "abcd",
            "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        }
        text, _ = await self.service.run_turn(
            self.conversation, self.turn, self.data, "confirm abcd"
        )
        self.assertIn("expired", text)
        self.model.respond.assert_not_awaited()

    async def test_wrong_confirmation_token_does_not_execute(self):
        self.conversation.pending = {
            "token": "abcd",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }
        text, _ = await self.service.run_turn(
            self.conversation, self.turn, self.data, "confirm wrong"
        )
        self.assertIn("does not match", text)
        self.model.respond.assert_not_awaited()

    async def test_invented_record_id_never_reaches_write(self):
        self.model.respond.side_effect = [
            reply(
                tool="change_record",
                arguments={"kind": "task", "record_id": str(uuid4()), "action": "complete"},
            ),
            reply(text="Please select a task."),
        ]
        text, evidence = await self.service.run_turn(
            self.conversation, self.turn, self.data, "Mark that done"
        )
        self.assertIn("Retrieve", evidence[0]["result"]["error"])
        self.assertIn("select", text)

    async def test_tool_loop_is_bounded(self):
        self.model.respond.return_value = reply(tool="list_records", arguments={"kind": "shopping"})
        text, _ = await self.service.run_turn(
            self.conversation, self.turn, self.data, "What is there?"
        )
        self.assertEqual(self.model.respond.await_count, 5)
        self.assertIn("limit", text)
