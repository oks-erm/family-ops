"""Run against an explicitly selected, migrated disposable PostgreSQL database."""

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.models import (
    AssistantAction,
    AssistantBudget,
    AssistantModelCall,
    FinancialTransaction,
    Household,
    HouseholdMember,
    ShoppingItem,
    ShoppingItemStatus,
    Task,
    TaskStatus,
    User,
)
from app.db.repositories.assistant_data import AssistantDataRepository, RecordNotFound
from app.db.repositories.conversations import (
    BudgetExceeded,
    ConversationBusy,
    ConversationRepository,
)
from app.schemas.conversation import DayPlan, FinanceQuery, ListRecords, SavePlanning
from app.services.conversation.service import ConversationService
from app.services.conversation.tools import HouseholdTools

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(
    DATABASE_URL, "Set TEST_DATABASE_URL to an isolated migrated PostgreSQL database"
)
class ConversationDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not make_url(DATABASE_URL).database.endswith("_test"):
            self.fail("Integration tests require a database name ending in _test")
        self.engine = create_async_engine(DATABASE_URL)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.factory()
        self.user = User(
            telegram_user_id=uuid4().int % 10**15,
            telegram_chat_id=uuid4().int % 10**15,
            timezone="Europe/Lisbon",
        )
        self.household = Household(name="Synthetic test household", invite_code=uuid4().hex)
        self.session.add_all([self.user, self.household])
        await self.session.flush()
        self.session.add(HouseholdMember(user_id=self.user.id, household_id=self.household.id))
        await self.session.commit()
        self.data = AssistantDataRepository(
            self.session,
            user_id=self.user.id,
            household_id=self.household.id,
            timezone="Europe/Lisbon",
        )
        self.repo = ConversationRepository(self.session)
        self.conversation = await self.repo.conversation(self.user.id, self.household.id, "test", 7)
        self.turn, _ = await self.repo.start_turn(self.conversation.id, "1")
        self.tools = HouseholdTools(self.data, self.repo, self.conversation, self.turn, set())

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_create_is_atomic_with_action_receipt_and_duplicate_safe(self):
        args = {
            "items": [
                {"kind": "shopping", "title": "milk"},
                {"kind": "task", "title": "Call dentist"},
            ]
        }
        first = await self.tools.execute("create_records", args)
        second = await self.tools.execute("create_records", args)
        self.assertEqual(first, second)
        self.assertEqual(
            await self.session.scalar(
                select(func.count())
                .select_from(ShoppingItem)
                .where(ShoppingItem.household_id == self.household.id)
            ),
            1,
        )
        self.assertEqual(
            await self.session.scalar(
                select(func.count())
                .select_from(AssistantAction)
                .where(AssistantAction.turn_id == self.turn.id)
            ),
            1,
        )

    async def test_duplicate_telegram_message_is_not_replayed_after_restart(self):
        settings = Settings(_env_file=None)
        first = await ConversationService(self.session, settings).handle(
            user_id=self.user.id, text="shopping: oranges", channel_key="test", message_key="42"
        )
        async with self.factory() as fresh:
            second = await ConversationService(fresh, settings).handle(
                user_id=self.user.id, text="shopping: oranges", channel_key="test", message_key="42"
            )
        self.assertEqual(first, second)
        self.assertEqual(
            await self.session.scalar(
                select(func.count())
                .select_from(ShoppingItem)
                .where(ShoppingItem.household_id == self.household.id)
            ),
            1,
        )

    async def test_other_households_records_cannot_be_read_or_edited(self):
        other = Household(name="Other test household", invite_code=uuid4().hex)
        self.session.add(other)
        await self.session.flush()
        item = ShoppingItem(
            user_id=self.user.id,
            household_id=other.id,
            name="Private item",
            status=ShoppingItemStatus.pending,
        )
        self.session.add(item)
        await self.session.commit()
        with self.assertRaises(RecordNotFound):
            await self.data.get_record("shopping", item.id)
        result = await self.data.list_records(ListRecords(kind="shopping"))
        self.assertEqual(result["records"], [])

    async def test_other_users_tasks_are_not_visible_in_same_household(self):
        other_user = User(telegram_user_id=uuid4().int % 10**15)
        self.session.add(other_user)
        await self.session.flush()
        self.session.add(
            Task(
                user_id=other_user.id,
                household_id=self.household.id,
                title="Private task",
                status=TaskStatus.pending,
            )
        )
        await self.session.commit()
        result = await self.data.list_records(ListRecords(kind="tasks"))
        self.assertEqual(result["records"], [])

    async def test_removal_requires_confirmation_and_changed_record_invalidates_it(self):
        created = await self.tools.execute(
            "create_records", {"items": [{"kind": "shopping", "title": "milk"}]}
        )
        record_id = created["records"][0]["id"]
        args = {"kind": "shopping", "record_id": record_id, "action": "remove"}
        proposed = await self.tools.execute("change_record", args)
        self.assertTrue(proposed["confirmation"])
        expected = self.conversation.pending["fingerprint"]
        await self.tools.execute(
            "change_record",
            {"kind": "shopping", "record_id": record_id, "action": "rename", "value": "oat milk"},
        )
        result = await self.tools.execute("change_record", args, confirmed=True, expected=expected)
        self.assertIn("changed", result["error"])
        current = await self.data.list_records(ListRecords(kind="shopping"))
        self.assertEqual(current["total"], 1)

    async def test_matching_confirmation_is_applied_once(self):
        created = await self.tools.execute(
            "create_records", {"items": [{"kind": "shopping", "title": "milk"}]}
        )
        args = {"kind": "shopping", "record_id": created["records"][0]["id"], "action": "remove"}
        await self.tools.execute("change_record", args)
        expected = self.conversation.pending["fingerprint"]
        result = await self.tools.execute("change_record", args, confirmed=True, expected=expected)
        self.assertNotIn("error", result)
        self.assertEqual((await self.data.list_records(ListRecords(kind="shopping")))["total"], 0)

    async def test_concurrent_turns_are_serialized(self):
        async with self.repo.lock(self.user.id, "test"):
            async with self.factory() as other:
                with self.assertRaises(ConversationBusy):
                    async with ConversationRepository(other).lock(self.user.id, "test"):
                        self.fail("Second lock must not be acquired")
        async with self.repo.lock(self.user.id, "test"):
            pass

    async def test_budget_reservations_do_not_overspend_concurrently(self):
        async def reserve():
            async with self.factory() as session:
                try:
                    await ConversationRepository(session).reserve(
                        self.household.id, self.turn.id, "fake", "everyday", 60, 100, "test"
                    )
                    return True
                except BudgetExceeded:
                    return False

        results = await asyncio.gather(reserve(), reserve())
        self.assertEqual(sorted(results), [False, True])
        amount = await self.session.scalar(
            select(AssistantBudget.tokens).where(AssistantBudget.household_id == self.household.id)
        )
        self.assertEqual(amount, 60)

    async def test_usage_settlement_records_actual_tokens(self):
        call, month = await self.repo.reserve(
            self.household.id, self.turn.id, "fake", "everyday", 100, 200, "test"
        )
        await self.repo.settle(
            call,
            month,
            {"input_tokens": 20, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 5}},
            100,
            "completed",
        )
        self.assertEqual(
            await self.session.scalar(
                select(AssistantBudget.tokens).where(
                    AssistantBudget.household_id == self.household.id
                )
            ),
            30,
        )
        self.assertEqual(call.cached_tokens, 5)

    async def test_finance_totals_are_exact_separate_currencies_and_untruncated(self):
        today = datetime.now(UTC).date()
        for _ in range(305):
            self.session.add(
                FinancialTransaction(
                    user_id=self.user.id,
                    household_id=self.household.id,
                    transaction_type="expense",
                    category="Gas",
                    description="Test petrol",
                    amount="0.10",
                    currency="EUR",
                    occurred_on=today,
                    source="manual",
                    raw_data={},
                )
            )
        self.session.add(
            FinancialTransaction(
                user_id=self.user.id,
                household_id=self.household.id,
                transaction_type="expense",
                category="Gas",
                description="Test petrol",
                amount="4.25",
                currency="USD",
                occurred_on=today,
                source="manual",
                raw_data={},
            )
        )
        await self.session.commit()
        result = await self.data.finance_query(FinanceQuery(start_date=today, end_date=today))
        totals = {r["currency"]: r["amount"] for r in result["totals"]}
        self.assertEqual(float(totals["EUR"]), 30.5)
        self.assertEqual(float(totals["USD"]), 4.25)
        self.assertEqual(result["count"], 306)
        self.assertEqual(len(result["details"]), 30)
        self.assertEqual(result["next_offset"], 30)

    async def test_invalid_finance_amount_is_reported(self):
        today = datetime.now(UTC).date()
        self.session.add(
            FinancialTransaction(
                user_id=self.user.id,
                household_id=self.household.id,
                transaction_type="expense",
                category="Other",
                description="Bad import",
                amount="NaN",
                currency="EUR",
                occurred_on=today,
                source="manual",
                raw_data={},
            )
        )
        await self.session.commit()
        result = await self.data.finance_query(FinanceQuery(start_date=today, end_date=today))
        self.assertFalse(result["complete"])
        self.assertEqual(result["invalid_amounts"], 1)

    async def test_household_change_clears_old_context(self):
        self.conversation.history = [
            {
                "user": "private",
                "assistant": "private",
                "evidence": [],
                "at": datetime.now(UTC).isoformat(),
            }
        ]
        await self.session.commit()
        other = Household(name="New household", invite_code=uuid4().hex)
        self.session.add(other)
        await self.session.commit()
        conversation = await self.repo.conversation(self.user.id, other.id, "test", 7)
        self.assertEqual(conversation.history, [])

    async def test_planning_notes_append_unless_replacement_is_explicit(self):
        today = datetime.now(UTC).date()
        await self.data.save_planning(SavePlanning(day=today, note="Dentist at 10"))
        await self.session.commit()
        await self.data.save_planning(SavePlanning(day=today, note="Collect parcel"))
        await self.session.commit()
        current = await self.data.day_plan(DayPlan(day=today))
        self.assertEqual(current["planning"]["notes"], "Dentist at 10; Collect parcel")
        await self.data.save_planning(
            SavePlanning(day=today, note="Rest day", note_mode="replace")
        )
        await self.session.commit()
        current = await self.data.day_plan(DayPlan(day=today))
        self.assertEqual(current["planning"]["notes"], "Rest day")

    async def test_household_change_scrubs_old_turn_responses(self):
        self.turn.response = "Former household private records"
        await self.session.commit()
        other = Household(name="New household", invite_code=uuid4().hex)
        self.session.add(other)
        await self.session.commit()
        await self.repo.conversation(self.user.id, other.id, "test", 7)
        previous, _ = await self.repo.start_turn(self.conversation.id, "1")
        self.assertNotIn("private records", previous.response)

    async def test_expiry_retains_deduplication_and_usage_records(self):
        self.turn.created_at = datetime.now(UTC) - timedelta(days=8)
        self.turn.response = "Sensitive old response"
        await self.session.commit()
        await self.repo.reserve(
            self.household.id, self.turn.id, "fake", "everyday", 10, 100, "test"
        )
        await self.repo.conversation(self.user.id, self.household.id, "test", 7)
        turn, created = await self.repo.start_turn(self.conversation.id, "1")
        self.assertFalse(created)
        self.assertNotIn("Sensitive", turn.response)
        self.assertEqual(
            await self.session.scalar(
                select(func.count())
                .select_from(AssistantModelCall)
                .where(AssistantModelCall.turn_id == turn.id)
            ),
            1,
        )
