"""Exercise aiogram objects through real ingress, persistence, worker and outbox."""

import asyncio
import os
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from aiogram.types import Update
from sqlalchemy import select

from app.db.models import AssistantInbox, AssistantOutbox, ShoppingItem
from app.runtime import ingress
from app.services.conversation.worker import deliver_once, process_job
from tests import test_assistant_workers as fixtures
from tests.test_conversation import reply


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Requires isolated test database")
class TelegramBoundaryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.WorkerDatabaseTests.asyncSetUp
    asyncTearDown = fixtures.WorkerDatabaseTests.asyncTearDown
    family = fixtures.WorkerDatabaseTests.family

    def telegram_update(self, text):
        self.next_update += 1
        return Update.model_validate(
            {
                "update_id": self.next_update,
                "message": {
                    "message_id": self.next_update,
                    "date": int(datetime.now(UTC).timestamp()),
                    "from": {
                        "id": self.user.telegram_user_id,
                        "is_bot": False,
                        "first_name": "Synthetic",
                    },
                    "chat": {"id": self.user.telegram_chat_id, "type": "private"},
                    "text": text,
                },
            }
        )

    async def execute_next(self, model):
        owner = uuid4()
        job = await self.queue.claim(owner)
        self.assertIsNotNone(job)
        self.assertEqual(job.household_id, self.household.id)
        await process_job(
            self.queue,
            job,
            owner,
            self.factory,
            self.settings,
            AsyncMock(),
            AsyncMock(),
            model=model,
        )
        async with self.factory() as session:
            saved = await session.get(AssistantInbox, job.id)
            self.assertEqual(saved.status, "complete", saved.error_code)
            output = await session.scalar(
                select(AssistantOutbox).where(AssistantOutbox.inbox_id == job.id)
            )
            return output.body

    async def test_real_ingress_serialization_to_shopping_write_read_and_delivery(self):
        updates = [
            self.telegram_update("Buy black pepper and detergent for laundry"),
            self.telegram_update("Shopping list"),
        ]
        bot = SimpleNamespace(
            get_updates=AsyncMock(side_effect=[updates, asyncio.CancelledError()])
        )
        with patch(
            "app.runtime.create_dispatcher",
            return_value=SimpleNamespace(
                resolve_used_update_types=lambda: ["message", "callback_query"],
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await ingress(self.queue, bot, self.settings, {"at": 0})
        self.assertEqual(bot.get_updates.call_args.kwargs["offset"], self.next_update + 1)
        async with self.factory() as session:
            saved = await session.scalar(
                select(AssistantInbox).where(
                    AssistantInbox.update_id == updates[0].update_id,
                )
            )
            self.assertIn("from", saved.payload["message"])
            self.assertNotIn("from_user", saved.payload["message"])
        model = SimpleNamespace(
            respond=AsyncMock(
                return_value=reply(
                    tool="create_records",
                    arguments={
                        "request_complete": True,
                        "items": [
                            {"kind": "shopping", "title": "black pepper"},
                            {"kind": "shopping", "title": "detergent for laundry"},
                        ],
                    },
                )
            )
        )
        created = await self.execute_next(model)
        self.assertIn("black pepper", created)
        model.respond.assert_awaited_once()
        model.respond.reset_mock()
        listed = await self.execute_next(model)
        self.assertIn("black pepper", listed)
        self.assertIn("detergent for laundry", listed)
        model.respond.assert_not_awaited()
        delivery = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=7))
        )
        self.assertTrue(await deliver_once(self.queue, delivery, uuid4()))
        self.assertTrue(await deliver_once(self.queue, delivery, uuid4()))
        self.assertEqual(delivery.send_message.call_args.kwargs["text"], listed)
        await self.queue.ingest(
            [u.model_dump(mode="json", by_alias=True, exclude_none=True) for u in updates],
            v2_enabled=True,
        )
        self.assertIsNone(await self.queue.claim(uuid4()))
        async with self.factory() as session:
            items = (
                await session.scalars(
                    select(ShoppingItem).where(
                        ShoppingItem.household_id == self.household.id,
                    )
                )
            ).all()
            self.assertEqual(len(items), 2)

    async def test_older_python_field_payloads_keep_household_scope_and_process(self):
        update = self.telegram_update("Shopping list")
        await self.queue.ingest(
            [update.model_dump(mode="json", exclude_none=True)],
            v2_enabled=True,
        )
        model = SimpleNamespace(respond=AsyncMock())
        self.assertIn("0 pending", await self.execute_next(model))
        self.next_update += 1
        callback = Update.model_validate(
            {
                "update_id": self.next_update,
                "callback_query": {
                    "id": "synthetic",
                    "chat_instance": "synthetic",
                    "from": update.message.from_user.model_dump(),
                    "message": update.message.model_dump(mode="json", by_alias=True),
                    "data": "assistant:cancel:deadbeef",
                },
            }
        )
        await self.queue.ingest(
            [callback.model_dump(mode="json", exclude_none=True)],
            v2_enabled=True,
        )
        self.assertIn("no longer matches", await self.execute_next(model))
        model.respond.assert_not_awaited()
