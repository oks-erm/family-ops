"""Synthetic reproductions of the reported chat failures and image-handler regressions."""

import os
import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from aiogram import Bot
from aiogram.types import File
from sqlalchemy import func, select

from app.db.models import (
    AssistantConversation,
    AssistantInbox,
    FinancialTransaction,
    PendingReceipt,
    PlanningConversation,
    Receipt,
    ReceiptItem,
    ShoppingItem,
    ShoppingItemStatus,
)
from app.db.repositories.assistant_data import AssistantDataRepository
from app.schemas.conversation import PurchaseHistory, SavePlanning, SaveWorkSchedule
from app.services.ai_router import AiJsonResult, AiProvider
from app.services.conversation.confirmations import confirmation_buttons
from app.services.conversation.service import ConversationService
from app.services.conversation.worker import process_job
from app.services.receipt_service import ReceiptService
from tests import test_assistant_workers as fixtures
from tests.test_conversation import reply


@unittest.skipUnless(
    os.environ.get("TEST_DATABASE_URL"), "Requires migrated isolated test database"
)
class ScreenshotDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.WorkerDatabaseTests.asyncSetUp
    asyncTearDown = fixtures.WorkerDatabaseTests.asyncTearDown
    family = fixtures.WorkerDatabaseTests.family
    ingest = fixtures.WorkerDatabaseTests.ingest

    async def process(self, text, model):
        await self.ingest(text)
        owner = uuid4()
        job = await self.queue.claim(owner)
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

    async def test_copy_confirmation_and_button_apply_exactly_once(self):
        model = SimpleNamespace(respond=AsyncMock())
        await self.process("shopping: tamari", model)
        async with self.factory() as session:
            item = await session.scalar(
                select(ShoppingItem).where(ShoppingItem.household_id == self.household.id)
            )
            item_id = item.id
        model.respond.side_effect = [
            reply(tool="list_records", arguments={"kind": "shopping"}),
            reply(
                tool="change_record",
                arguments={"kind": "shopping", "record_id": str(item_id), "action": "remove"},
            ),
        ]
        await self.process("Remove the tamari", model)
        async with self.factory() as session:
            conversation = await session.scalar(
                select(AssistantConversation).where(AssistantConversation.user_id == self.user.id)
            )
            token = conversation.pending["token"]
            expiry = conversation.pending["expires_at"]
        model.respond.reset_mock()
        await self.process("Output the exact reply so I could copy it", model)
        model.respond.assert_not_awaited()
        async with self.factory() as session:
            conversation = await session.get(AssistantConversation, conversation.id)
            self.assertEqual(conversation.pending["token"], token)
            self.assertEqual(conversation.pending["expires_at"], expiry)
        markup = confirmation_buttons(f"confirm {token}")
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, f"assistant:confirm:{token}")
        self.next_update += 1
        payload = {
            "update_id": self.next_update,
            "callback_query": {
                "id": "synthetic-callback",
                "chat_instance": "synthetic",
                "from": {
                    "id": self.user.telegram_user_id,
                    "is_bot": False,
                    "first_name": "Synthetic",
                },
                "message": {
                    "message_id": 123,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": self.user.telegram_chat_id, "type": "private"},
                },
                "data": f"assistant:confirm:{token}",
            },
        }
        await self.queue.ingest([payload], v2_enabled=True)
        owner = uuid4()
        job = await self.queue.claim(owner)
        self.assertTrue(job.replay_safe)
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
        await self.queue.ingest([payload], v2_enabled=True)
        self.assertIsNone(await self.queue.claim(uuid4()))
        async with self.factory() as session:
            self.assertEqual(
                (await session.get(ShoppingItem, item_id)).status, ShoppingItemStatus.skipped
            )
            self.assertIsNone((await session.get(AssistantConversation, conversation.id)).pending)
        model.respond.assert_not_awaited()

    async def test_full_month_is_one_atomic_tool_and_preserves_existing_notes(self):
        async with self.factory() as session:
            session.add(
                PlanningConversation(
                    user_id=self.user.id,
                    household_id=self.household.id,
                    plan_date=date(2026, 10, 5),
                    unusual_notes="Keep this note",
                    state="awaiting_work_start",
                    raw_notes=[],
                )
            )
            await session.commit()
        model = SimpleNamespace(
            respond=AsyncMock(
                return_value=reply(
                    tool="save_work_schedule",
                    arguments={
                        "start_date": "2026-10-01",
                        "end_date": "2026-10-31",
                        "weekdays": [1, 2, 3, 4, 5],
                        "work_start": "08:30",
                        "work_end": "18:00",
                        "request_complete": True,
                    },
                )
            )
        )
        async with self.factory() as session:
            service = ConversationService(session, self.settings, model=model)
            result = await service.handle(
                user_id=self.user.id,
                text="All October",
                channel_key="schedule-test",
                message_key="1",
            )
        self.assertIn("22 days", result)
        model.respond.assert_awaited_once()
        async with self.factory() as session:
            plans = (
                await session.scalars(
                    select(PlanningConversation).where(
                        PlanningConversation.household_id == self.household.id
                    )
                )
            ).all()
            self.assertEqual(len(plans), 22)
            self.assertTrue(
                all(
                    p.plan_date.weekday() < 5
                    and str(p.work_start) == "08:30:00"
                    and str(p.work_end) == "18:00:00"
                    for p in plans
                )
            )
            self.assertEqual(
                next(p for p in plans if p.plan_date.day == 5).unusual_notes, "Keep this note"
            )
            result2 = await ConversationService(session, self.settings, model=model).handle(
                user_id=self.user.id,
                text="All October",
                channel_key="schedule-test",
                message_key="1",
            )
            self.assertEqual(result, result2)
        model.respond.assert_awaited_once()

    async def test_purchase_frequency_aggregates_all_receipts_without_other_household_data(self):
        other, other_household = await self.family()
        async with self.factory() as session:
            for user, household, day, names in [
                (self.user, self.household, 1, ["Milk", " Milk ", "Eggs"]),
                (self.user, self.household, 8, ["milk"]),
                (self.user, self.household, 15, ["MILK"]),
                (other, other_household, 20, ["Private other purchase"]),
            ]:
                receipt = Receipt(
                    user_id=user.id, household_id=household.id, purchased_at=date(2026, 9, day)
                )
                session.add(receipt)
                await session.flush()
                session.add_all([ReceiptItem(receipt_id=receipt.id, name=name) for name in names])
            session.add(
                ShoppingItem(user_id=self.user.id, household_id=self.household.id, name="milk")
            )
            await session.commit()
            data = AssistantDataRepository(
                session, user_id=self.user.id, household_id=self.household.id, timezone="UTC"
            )
            result = await data.purchase_history(
                PurchaseHistory(start_date=date(2026, 9, 1), end_date=date(2026, 9, 30))
            )
            self.assertEqual(result["receipt_count"], 3)
            self.assertEqual(result["distinct_items"], 2)
            milk = result["items"][0]
            self.assertEqual(milk["purchases"], 3)
            self.assertEqual(milk["typical_gap_days"], 7)
            self.assertTrue(milk["already_on_list"])
            self.assertNotIn("Private", str(result))
            empty = await data.purchase_history(
                PurchaseHistory(start_date=date(2026, 1, 1), end_date=date(2026, 1, 31))
            )
            self.assertEqual(empty["items"], [])

    async def test_image_pipeline_receipt_preview_confirm_and_bank_document(self):
        from app.bot.main import create_dispatcher

        dispatcher = create_dispatcher()
        bot = Bot(token="123456:synthetic-test-token")
        receipt_data = {
            "shop_name": "Synthetic <shop>",
            "currency": "EUR",
            "total_amount": "3.50",
            "purchased_at": "2026-09-30",
            "items": [{"name": "Milk", "total_amount": "3.50"}],
        }
        bank_data = {
            "transactions": [
                {
                    "description": "Synthetic groceries",
                    "amount": "12.50",
                    "occurred_on": "2026-09-30",
                    "transaction_type": "expense",
                    "category": "Food",
                }
            ]
        }

        async def download(path, destination, **kw):
            destination.write(b"synthetic image bytes")

        empty_bank = AiJsonResult(provider=AiProvider.gemini, data={"transactions": []})
        with (
            patch("app.bot.handlers.receipts.async_session_factory", self.factory),
            patch("app.bot.handlers.receipts.get_settings", return_value=self.settings),
            patch.object(
                bot,
                "get_file",
                AsyncMock(
                    return_value=File(
                        file_id="synthetic",
                        file_unique_id="x",
                        file_path="synthetic.jpg",
                        file_size=20,
                    )
                ),
            ),
            patch.object(bot, "download_file", AsyncMock(side_effect=download)),
            patch("aiogram.Bot.__call__", new=AsyncMock(return_value=True)) as sends,
            patch(
                "app.services.ai_router.AiRouter.extract_bank_transactions",
                new=AsyncMock(
                    side_effect=[
                        empty_bank,
                        AiJsonResult(provider=AiProvider.gemini, data=bank_data),
                    ]
                ),
            ) as banks,
            patch(
                "app.services.ai_router.AiRouter.extract_receipt",
                new=AsyncMock(
                    return_value=AiJsonResult(provider=AiProvider.gemini, data=receipt_data)
                ),
            ) as receipts,
        ):
            for kind in ["photo", "document"]:
                payload = await self.ingest("placeholder")
                payload["message"].pop("text")
                if kind == "photo":
                    payload["message"]["photo"] = [
                        {"file_id": "synthetic", "file_unique_id": "x", "width": 100, "height": 100}
                    ]
                else:
                    payload["message"]["document"] = {
                        "file_id": "synthetic",
                        "file_unique_id": "x",
                        "mime_type": "image/png",
                    }
                async with self.factory() as session:
                    job = await session.scalar(
                        select(AssistantInbox).where(
                            AssistantInbox.update_id == payload["update_id"]
                        )
                    )
                    job.payload, job.replay_safe = payload, False
                    await session.commit()
                owner = uuid4()
                job = await self.queue.claim(owner)
                await process_job(
                    self.queue, job, owner, self.factory, self.settings, bot, dispatcher
                )
                async with self.factory() as session:
                    self.assertEqual((await session.get(AssistantInbox, job.id)).status, "complete")
            receipts.assert_awaited_once()
            self.assertEqual(banks.await_count, 2)
            self.assertEqual(banks.call_args.kwargs["mime_type"], "image/png")
            self.assertTrue(all(call.args[0].parse_mode is None for call in sends.call_args_list))
        async with self.factory() as session:
            pending = await session.scalar(
                select(PendingReceipt).where(PendingReceipt.household_id == self.household.id)
            )
            self.assertEqual(pending.image_path, "")
            self.assertEqual(
                await session.scalar(
                    select(func.count())
                    .select_from(Receipt)
                    .where(Receipt.household_id == self.household.id)
                ),
                0,
            )
            service = ReceiptService(session, self.settings)
            first = await service.confirm_pending_receipt(
                pending_receipt_id=pending.id, telegram_user_id=self.user.telegram_user_id
            )
            self.assertIn("Receipt saved", first)
            second = await service.confirm_pending_receipt(
                pending_receipt_id=pending.id, telegram_user_id=self.user.telegram_user_id
            )
            self.assertIn("no longer available", second)
            self.assertEqual(
                await session.scalar(
                    select(func.count())
                    .select_from(Receipt)
                    .where(Receipt.household_id == self.household.id)
                ),
                1,
            )
            self.assertEqual(
                await session.scalar(
                    select(func.count())
                    .select_from(FinancialTransaction)
                    .where(FinancialTransaction.household_id == self.household.id)
                ),
                1,
            )
        await bot.session.close()


class RangeValidationTests(unittest.TestCase):
    def test_invalid_ranges_and_weekdays_do_not_execute_partial_schedules(self):
        for fields in [
            {"weekdays": [0]},
            {"weekdays": [1, 1]},
            {"end_date": "2026-09-01"},
            {"end_date": "2028-01-01"},
            {"work_end": "07:00"},
            {"work_start": "08:30:00+00:00"},
        ]:
            values = {
                "start_date": "2026-10-01",
                "end_date": "2026-10-31",
                "weekdays": [1, 2, 3, 4, 5],
                "work_start": "08:30",
                "work_end": "18:00",
                **fields,
            }
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                SaveWorkSchedule.model_validate(values)

    def test_local_work_time_schema_and_runtime_agree(self):
        schema = SaveWorkSchedule.model_json_schema()["properties"]["work_start"]
        self.assertNotIn("format", schema)
        self.assertRegex("08:30", schema["pattern"])
        self.assertNotRegex("08:30:00+00:00", schema["pattern"])
        with self.assertRaises(ValueError):
            SavePlanning(day="2026-10-01", work_start="08:30:00+00:00")
