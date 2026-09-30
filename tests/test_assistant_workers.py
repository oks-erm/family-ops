"""Disposable PostgreSQL tests for message ordering, recovery and household fairness."""

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.models import (
    AssistantHouseholdPolicy,
    AssistantInbox,
    AssistantOutbox,
    Household,
    HouseholdMember,
    PendingReceipt,
    ShoppingItem,
    Task,
    User,
)
from app.db.repositories.assistant_queue import AssistantQueueRepository
from app.db.repositories.conversations import BudgetExceeded, ConversationRepository
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.leases import LeaseLost, LeaseRepository
from app.db.repositories.tasks import TaskRepository
from app.services.conversation.worker import deliver_once, process_job, process_text
from app.services.receipt_service import ReceiptService

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(DATABASE_URL, "Requires isolated migrated TEST_DATABASE_URL")
class WorkerDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue(make_url(DATABASE_URL).database.endswith("_test"))
        self.engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0, pool_timeout=3)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.queue = AssistantQueueRepository(self.factory)
        self.settings = Settings(_env_file=None, ASSISTANT_V2_ENABLED=True)
        self.users, self.households, self.chats = [], [], []
        self.next_update = max(await self.queue.offset(), 1000)
        self.user, self.household = await self.family()

    async def family(self):
        async with self.factory() as session:
            user = User(telegram_user_id=uuid4().int % 10**14, timezone="Europe/Lisbon")
            user.telegram_chat_id = user.telegram_user_id
            household = Household(name="Synthetic worker test", invite_code=uuid4().hex.upper())
            session.add_all([user, household])
            await session.flush()
            session.add(HouseholdMember(user_id=user.id, household_id=household.id))
            await session.commit()
            self.users.append(user.id)
            self.households.append(household.id)
            self.chats.append(user.telegram_chat_id)
            return user, household

    async def asyncTearDown(self):
        # Only this test's explicitly created fixtures, never a default or production DB.
        async with self.factory() as session:
            await session.execute(
                delete(AssistantInbox).where(AssistantInbox.chat_id.in_(self.chats))
            )
            await session.execute(delete(Household).where(Household.id.in_(self.households)))
            await session.execute(delete(User).where(User.id.in_(self.users)))
            await session.commit()
        await self.engine.dispose()

    async def ingest(self, message="shopping: milk", user=None):
        user = user or self.user
        self.next_update += 1
        payload = {
            "update_id": self.next_update,
            "message": {
                "message_id": self.next_update,
                "date": int(datetime.now(UTC).timestamp()),
                "from": {"id": user.telegram_user_id, "is_bot": False, "first_name": "Synthetic"},
                "chat": {"id": user.telegram_chat_id, "type": "private"},
                "text": message,
            },
        }
        await self.queue.ingest([payload], v2_enabled=True)
        return payload

    async def test_ingestion_is_durable_and_duplicate_safe(self):
        payload = await self.ingest()
        await self.queue.ingest([payload], v2_enabled=True)
        self.assertEqual(await self.queue.offset(), payload["update_id"] + 1)
        async with self.factory() as session:
            self.assertEqual(
                await session.scalar(
                    select(func.count())
                    .select_from(AssistantInbox)
                    .where(AssistantInbox.chat_id == self.user.telegram_chat_id)
                ),
                1,
            )

    async def test_same_conversation_queues_other_households_continue(self):
        other, _ = await self.family()
        await self.ingest("shopping: first")
        await self.ingest("shopping: second")
        await self.ingest("shopping: other", other)
        owner = uuid4()
        first = await self.queue.claim(owner)
        second = await self.queue.claim(uuid4())
        self.assertEqual(first.payload["message"]["text"], "shopping: first")
        self.assertEqual(second.chat_id, other.telegram_chat_id)
        self.assertIsNone(await self.queue.claim(uuid4()))
        await self.queue.finish(first.id, owner, "First completed")
        third = await self.queue.claim(uuid4())
        self.assertEqual(third.payload["message"]["text"], "shopping: second")

    async def test_concurrent_claims_respect_global_limit(self):
        for _ in range(4):
            user, _ = await self.family()
            await self.ingest(user=user)
        jobs = await asyncio.gather(*(self.queue.claim(uuid4(), global_limit=2) for _ in range(6)))
        self.assertEqual(sum(job is not None for job in jobs), 2)

    async def test_rate_limited_household_does_not_block_other_household(self):
        other, _ = await self.family()
        await self.ingest("shopping: first")
        await self.ingest("shopping: second")
        await self.ingest(user=other)
        async with self.factory() as session:
            await session.execute(
                update(AssistantHouseholdPolicy)
                .where(AssistantHouseholdPolicy.household_id == self.household.id)
                .values(requests_per_minute=1)
            )
            await session.commit()
        owner = uuid4()
        first = await self.queue.claim(owner)
        await self.queue.finish(first.id, owner)
        self.assertEqual((await self.queue.claim(uuid4())).chat_id, other.telegram_chat_id)
        self.assertIsNone(await self.queue.claim(uuid4()))

    async def test_crash_after_saved_turn_recovers_receipt_without_duplicate_write(self):
        payload = await self.ingest()
        owner = uuid4()
        job = await self.queue.claim(owner)
        original = await process_text(payload, self.factory, self.settings)
        async with self.factory() as session:
            await session.execute(
                update(AssistantInbox)
                .where(AssistantInbox.id == job.id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        new_owner = uuid4()
        recovered = await self.queue.claim(new_owner)
        self.assertEqual(recovered.id, job.id)
        await process_job(
            self.queue, recovered, new_owner, self.factory, self.settings, AsyncMock(), AsyncMock()
        )
        async with self.factory() as session:
            self.assertEqual(
                await session.scalar(
                    select(func.count())
                    .select_from(ShoppingItem)
                    .where(ShoppingItem.household_id == self.household.id)
                ),
                1,
            )
            self.assertEqual(
                await session.scalar(
                    select(AssistantOutbox.body).where(AssistantOutbox.inbox_id == job.id)
                ),
                original,
            )

    async def test_expired_legacy_job_is_not_replayed(self):
        await self.ingest("/join SYNTHETIC")
        job = await self.queue.claim(uuid4())
        async with self.factory() as session:
            await session.execute(
                update(AssistantInbox)
                .where(AssistantInbox.id == job.id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        self.assertIsNone(await self.queue.claim(uuid4()))
        async with self.factory() as session:
            self.assertEqual((await session.get(AssistantInbox, job.id)).status, "uncertain")

    async def test_old_owner_cannot_finish_or_release_replacement_lease(self):
        await self.ingest()
        old, new = uuid4(), uuid4()
        job = await self.queue.claim(old)
        async with self.factory() as session:
            await session.execute(
                update(AssistantInbox)
                .where(AssistantInbox.id == job.id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        replacement = await self.queue.claim(new)
        self.assertEqual(replacement.id, job.id)
        self.assertFalse(await self.queue.renew(job.id, old))
        with self.assertRaises(LeaseLost):
            await self.queue.finish(job.id, old, "Stale reply")
        await self.queue.finish(job.id, new, "Current reply")

    async def test_conversation_leases_do_not_exhaust_small_connection_pool(self):
        entered = 0
        release = asyncio.Event()

        async def hold(number):
            nonlocal entered
            async with LeaseRepository(self.engine).held(f"test:{self.user.id}:{number}"):
                entered += 1
                if entered == 20:
                    release.set()
                await asyncio.wait_for(release.wait(), timeout=5)

        await asyncio.gather(*(hold(i) for i in range(20)))
        self.assertEqual(entered, 20)

    async def test_reply_chunks_are_ordered_and_unknown_delivery_is_not_retried(self):
        await self.ingest()
        owner = uuid4()
        job = await self.queue.claim(owner)
        await self.queue.finish(job.id, owner, "x" * 3600)
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError))
        await deliver_once(self.queue, bot, uuid4())
        self.assertIsNone(await self.queue.claim_delivery(uuid4()))
        bot.send_message.assert_awaited_once()
        async with self.factory() as session:
            states = (
                await session.scalars(
                    select(AssistantOutbox.status)
                    .where(AssistantOutbox.inbox_id == job.id)
                    .order_by(AssistantOutbox.part)
                )
            ).all()
            self.assertEqual(states, ["uncertain", "failed"])

    async def test_successful_reply_is_never_claimed_twice(self):
        await self.ingest()
        owner = uuid4()
        job = await self.queue.claim(owner)
        await self.queue.finish(job.id, owner, "A saved reply")
        delivery = await self.queue.claim_delivery(owner)
        self.assertIsNone(await self.queue.claim_delivery(uuid4()))
        await self.queue.delivered(delivery.id, owner, telegram_id=123)
        self.assertIsNone(await self.queue.claim_delivery(uuid4()))

    async def test_household_dollar_limit_is_atomic_and_unknown_models_fail_closed(self):
        async with self.factory() as session:
            repo = ConversationRepository(session)
            conversation = await repo.conversation(self.user.id, self.household.id, "budget", 7)
            turn, _ = await repo.start_turn(conversation.id, "1")
            session.add(
                AssistantHouseholdPolicy(
                    household_id=self.household.id, monthly_usd_limit=Decimal("0.00015")
                )
            )
            await session.commit()
            turn_id = turn.id

        async def reserve():
            async with self.factory() as session:
                try:
                    await ConversationRepository(session).reserve(
                        self.household.id, turn_id, "gpt-6-luna", "everyday", 1000, 10000, "test"
                    )
                    return True
                except BudgetExceeded:
                    return False

        self.assertEqual(sorted(await asyncio.gather(reserve(), reserve())), [False, True])
        async with self.factory() as session:
            with self.assertRaises(BudgetExceeded):
                await ConversationRepository(session).reserve(
                    self.household.id, turn_id, "unknown", "everyday", 1, 10000, "test"
                )

    async def test_join_preserves_old_records_and_rejects_old_task_and_receipt_actions(self):
        _, new_household = await self.family()
        async with self.factory() as session:
            user = await session.get(User, self.user.id)
            task = Task(
                user_id=user.id, household_id=self.household.id, title="Original family task"
            )
            receipt = PendingReceipt(
                user_id=user.id,
                household_id=self.household.id,
                telegram_chat_id=user.telegram_chat_id,
                image_path="/unused",
                mime_type="image/jpeg",
                extraction={},
            )
            session.add_all([task, receipt])
            await session.commit()
            await HouseholdRepository(session).join_by_invite_code(
                user=user, invite_code=new_household.invite_code
            )
            await session.refresh(task)
            self.assertEqual(task.household_id, self.household.id)
            self.assertIsNone(
                await TaskRepository(session).get_user_task(task_id=task.id, user_id=user.id)
            )
            service = ReceiptService(session, self.settings)
            self.assertIn(
                "no longer available",
                await service.confirm_pending_receipt(
                    pending_receipt_id=receipt.id, telegram_user_id=user.telegram_user_id
                ),
            )
            self.assertIsNotNone(await session.get(PendingReceipt, receipt.id))

    async def test_join_rebinds_already_queued_messages_to_new_household_limits(self):
        _, target = await self.family()
        await self.ingest("/join " + target.invite_code)
        await self.ingest("shopping: bread")
        owner = uuid4()
        job = await self.queue.claim(owner)
        async with self.factory() as session:
            joined = await HouseholdRepository(session).join_by_invite_code(
                user=await session.get(User, self.user.id), invite_code=target.invite_code
            )
            self.assertIsNotNone(joined)
        await self.queue.finish(job.id, owner)
        next_job = await self.queue.claim(uuid4())
        self.assertEqual(next_job.household_id, target.id)

    async def test_last_reply_cannot_read_previous_household(self):
        payload = await self.ingest()
        await process_text(payload, self.factory, self.settings)
        other, target = await self.family()
        async with self.factory() as session:
            await HouseholdRepository(session).join_by_invite_code(
                user=await session.get(User, self.user.id), invite_code=target.invite_code
            )
        payload["message"]["text"] = "/last_reply"
        response = await process_text(payload, self.factory, self.settings)
        self.assertIn("no recent saved reply", response)

    async def test_expired_conversation_owner_cannot_mutate_data(self):
        from app.db.models import RuntimeLease

        name = f"test:fence:{self.user.id}"
        old, new = uuid4(), uuid4()
        leases = LeaseRepository(self.engine)
        self.assertTrue(await leases.acquire(name, old, 60))
        async with self.factory() as session:
            await session.execute(
                update(RuntimeLease)
                .where(RuntimeLease.name == name)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        self.assertTrue(await leases.acquire(name, new, 60))
        await leases.release(name, old)
        async with self.factory() as session:
            with self.assertRaises(LeaseLost):
                await leases.fence(session, name, old)
        self.assertTrue(await leases.renew(name, new, 60))

    async def test_explicit_telegram_rate_limit_can_retry_without_reexecuting_job(self):
        from aiogram.exceptions import TelegramRetryAfter
        from aiogram.methods import SendMessage

        await self.ingest()
        owner = uuid4()
        job = await self.queue.claim(owner)
        await self.queue.finish(job.id, owner, "Saved reply")
        bot = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=TelegramRetryAfter(
                    method=SendMessage(chat_id=1, text="synthetic"),
                    message="slow down",
                    retry_after=1,
                )
            )
        )
        await deliver_once(self.queue, bot, uuid4())
        self.assertIsNone(await self.queue.claim_delivery(uuid4()))
        async with self.factory() as session:
            await session.execute(
                update(AssistantOutbox)
                .where(AssistantOutbox.inbox_id == job.id)
                .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        bot.send_message.side_effect = None
        bot.send_message.return_value = SimpleNamespace(message_id=123)
        await deliver_once(self.queue, bot, uuid4())
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertIsNone(await self.queue.claim_delivery(uuid4()))

    async def test_join_keeps_planning_history_and_allows_independent_same_day_plan(self):
        from app.db.models import DailyPlan, DailyPlanStatus, PlanningConversation
        from app.db.repositories.assistant_data import AssistantDataRepository
        from app.db.repositories.planning import PlanningRepository
        from app.schemas.conversation import SavePlanning

        _, target = await self.family()
        day = datetime.now(UTC).date()
        async with self.factory() as session:
            plans = PlanningRepository(session)
            original = await plans.start_conversation(
                user_id=self.user.id, household_id=self.household.id, plan_date=day
            )
            original.unusual_notes = "Original household private note"
            await session.commit()
            original_id = original.id
            await plans.upsert_daily_plan(
                user_id=self.user.id,
                household_id=self.household.id,
                plan_date=day,
                work_start=None,
                work_end=None,
                unusual_notes="Original",
                plan={},
                status=DailyPlanStatus.draft,
            )
            joined = await HouseholdRepository(session).join_by_invite_code(
                user=await session.get(User, self.user.id), invite_code=target.invite_code
            )
            self.assertIsNotNone(joined)
            self.assertIsNone(await plans.get_active_conversation(user_id=self.user.id))
            self.assertIsNone(await plans.get_daily_plan(user_id=self.user.id, plan_date=day))
            await AssistantDataRepository(
                session, user_id=self.user.id, household_id=target.id, timezone="UTC"
            ).save_planning(SavePlanning(day=day, note="New household note"))
            await session.commit()
            await plans.upsert_daily_plan(
                user_id=self.user.id,
                household_id=target.id,
                plan_date=day,
                work_start=None,
                work_end=None,
                unusual_notes="New",
                plan={},
                status=DailyPlanStatus.draft,
            )
            old = await session.get(PlanningConversation, original_id)
            self.assertEqual(old.household_id, self.household.id)
            self.assertEqual(old.unusual_notes, "Original household private note")
            current = await plans.get_conversation(user_id=self.user.id, plan_date=day)
            self.assertEqual(current.household_id, target.id)
            self.assertEqual(current.unusual_notes, "New household note")
            self.assertEqual(
                await session.scalar(
                    select(func.count())
                    .select_from(DailyPlan)
                    .where(DailyPlan.user_id == self.user.id)
                ),
                2,
            )
