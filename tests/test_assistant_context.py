import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx

from app.clients.conversation_model import InvalidModelResponse, OpenAIConversationModel
from app.schemas.conversation import ConversationAnswer
from app.services.conversation.context import update_context
from tests import test_conversation as baseline
from tests.test_conversation import reply, settings


class ContextTests(unittest.TestCase):
    def test_topic_switch_preserves_references_but_clears_old_question(self):
        now = datetime.now(UTC).isoformat()
        current = {"tasks": {"at": now, "references": [{"id": "task-id"}]}}
        question = ConversationAnswer(
            reply="Which period?", topic="finance", needs_clarification=True
        )
        state = update_context(current, [], answer=question)
        self.assertEqual(state["dialogue"]["question"], "Which period?")
        answer = ConversationAnswer(reply="Milk", topic="shopping", needs_clarification=False)
        state = update_context(state, [], answer=answer)
        self.assertIsNone(state["dialogue"]["question"])
        self.assertEqual(state["tasks"]["references"], [{"id": "task-id"}])

    def test_configured_retention_removes_stale_references(self):
        state = {
            "shopping": {
                "at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
                "references": [{"id": "stale"}],
            }
        }
        self.assertEqual(update_context(state, [], history_days=1), {})

    def test_compound_write_retains_both_topics_and_clears_outstanding_question(self):
        state = update_context(
            {},
            [],
            answer=ConversationAnswer(
                reply="What time?", topic="planning", needs_clarification=True
            ),
        )
        state = update_context(
            state,
            [
                {
                    "tool": "create_records",
                    "result": {
                        "records": [
                            {"id": "milk", "kind": "shopping", "title": "Milk"},
                            {"id": "call", "kind": "task", "title": "Call plumber"},
                        ]
                    },
                }
            ],
            clear_question=True,
        )
        self.assertEqual(state["shopping"]["references"][0]["id"], "milk")
        self.assertEqual(state["tasks"]["references"][0]["id"], "call")
        self.assertIsNone(state["dialogue"]["question"])

    def test_empty_list_retires_its_previous_references(self):
        state = {"shopping": {"at": datetime.now(UTC).isoformat(), "references": [{"id": "old"}]}}
        state = update_context(
            state,
            [{"tool": "list_records", "result": {"kind": "shopping", "records": [], "total": 0}}],
        )
        self.assertEqual(state["shopping"]["references"], [])


class StructuredClientTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, text, details=None):
        def handler(request):
            self.assertTrue(json.loads(request.content)["text"]["format"]["strict"])
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 5,
                        "input_tokens_details": details or {},
                    },
                    "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": text}]}
                    ],
                },
            )

        return await OpenAIConversationModel(
            settings(OPENAI_API_KEY="synthetic"), httpx.MockTransport(handler)
        ).respond(model="test", instructions="test", inputs=[], tools=[])

    async def test_validated_answer_exposes_only_reply(self):
        result = await self.call(
            json.dumps({"reply": "Which task?", "topic": "tasks", "needs_clarification": True})
        )
        self.assertEqual(result.text, "Which task?")
        self.assertTrue(result.answer.needs_clarification)

    async def test_invalid_structure_and_usage_are_rejected(self):
        for text, details in [("plain text", {}), ("{}", {}), ("{}", {"cached_tokens": 6})]:
            with self.subTest(text=text, details=details), self.assertRaises(InvalidModelResponse):
                await self.call(text, details)


class RoutingImprovementTests(unittest.IsolatedAsyncioTestCase):
    setUp = baseline.ConversationTests.setUp

    async def test_missing_reference_stays_on_cheap_model_and_asks(self):
        self.model.respond.side_effect = [
            reply(tool="escalate", arguments={"reason": "unresolved_reference"}),
            reply(text="Which task do you mean?"),
        ]
        text, _ = await self.service.run_turn(self.conversation, self.turn, self.data, "move that")
        self.assertIn("Which", text)
        self.assertEqual(
            {c.kwargs["model"] for c in self.model.respond.call_args_list}, {"gpt-6-luna"}
        )

    async def test_complete_write_skips_paraphrasing_call(self):
        self.model.respond.return_value = reply(tool="create_records", arguments={})
        with patch(
            "app.services.conversation.service.HouseholdTools.execute",
            new=AsyncMock(return_value={"message": "Added milk.", "request_complete": True}),
        ):
            text, _ = await self.service.run_turn(
                self.conversation, self.turn, self.data, "add milk please"
            )
        self.assertEqual(text, "Added milk.")
        self.assertEqual(self.model.respond.await_count, 1)

    async def test_partial_write_continues_until_compound_request_finishes(self):
        self.model.respond.side_effect = [
            reply(tool="create_records"),
            reply(tool="record_transaction"),
        ]
        with patch(
            "app.services.conversation.service.HouseholdTools.execute",
            new=AsyncMock(
                side_effect=[
                    {"message": "Added milk.", "request_complete": False},
                    {"message": "Recorded expense.", "request_complete": True},
                ]
            ),
        ):
            text, _ = await self.service.run_turn(
                self.conversation, self.turn, self.data, "add milk and record a 3 EUR expense"
            )
        self.assertIn("Added milk", text)
        self.assertIn("Recorded expense", text)
        self.assertEqual(self.model.respond.await_count, 2)
