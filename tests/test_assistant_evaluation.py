import unittest
from unittest.mock import AsyncMock

from app.clients.conversation_model import ModelUnavailable
from app.config import Settings
from scripts.evaluate_assistant import usage_cost
from scripts.evaluate_conversation_flow import BoundedModel


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_cost_accounts_for_cache_write_premium(self):
        cost = usage_cost("gpt-6-luna", {
            "input_tokens": 100, "output_tokens": 10,
            "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 30},
        })
        self.assertAlmostEqual(cost, 0.00001395)

    async def test_dollar_limit_stops_before_provider_call(self):
        model = BoundedModel(Settings(_env_file=None), 0.000001)
        model.client.respond = AsyncMock()
        with self.assertRaises(ModelUnavailable):
            await model.respond(model="gpt-6-luna", instructions="test", inputs=[], tools=[])
        model.client.respond.assert_not_awaited()

    async def test_unknown_provider_failure_retains_cost_reservation(self):
        model = BoundedModel(Settings(_env_file=None), 0.1)
        model.client.respond = AsyncMock(side_effect=ModelUnavailable("timeout"))
        with self.assertRaises(ModelUnavailable):
            await model.respond(model="gpt-6-luna", instructions="test", inputs=[], tools=[])
        self.assertGreater(model.total, 0)
