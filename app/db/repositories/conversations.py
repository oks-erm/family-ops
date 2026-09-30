"""Durable turns, deduplicated actions and atomic household token reservations."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import (
    AssistantAction,
    AssistantBudget,
    AssistantConversation,
    AssistantHouseholdPolicy,
    AssistantModelCall,
    AssistantTurn,
)
from app.db.repositories.leases import LeaseBusy, LeaseRepository
from app.services.conversation.pricing import estimate


class ConversationBusy(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class ConversationRepository:
    def __init__(self, session):
        self.session = session

    @asynccontextmanager
    async def lock(self, user_id: UUID, channel_key: str):
        name = f"conversation:{user_id}:{channel_key}"
        try:
            async with LeaseRepository(self.session.bind).held(name) as owner:
                self.session.info["conversation_lease"] = (name, owner)
                try:
                    yield
                finally:
                    await self.session.rollback()
                    self.session.info.pop("conversation_lease", None)
        except LeaseBusy as exc:
            raise ConversationBusy from exc

    async def fence(self):
        lease = self.session.info.get("conversation_lease")
        if lease:
            await LeaseRepository.fence(self.session, *lease)

    async def conversation(self, user_id, household_id, channel_key, history_days):
        await self.fence()
        await self.session.execute(
            insert(AssistantConversation)
            .values(
                user_id=user_id,
                household_id=household_id,
                channel_key=channel_key,
                history=[],
            )
            .on_conflict_do_nothing(constraint="uq_assistant_conversation")
        )
        conversation = await self.session.scalar(
            select(AssistantConversation).where(
                AssistantConversation.user_id == user_id,
                AssistantConversation.channel_key == channel_key,
            )
        )
        cutoff = datetime.now(UTC) - timedelta(days=history_days)
        changed_household = conversation.household_id != household_id
        # Joining a different household must not leak previous household context.
        if conversation.household_id != household_id or conversation.updated_at < cutoff:
            conversation.history = []
            conversation.pending = None
            conversation.context = {}
            conversation.household_id = household_id
        conversation.history = [
            entry for entry in conversation.history if datetime.fromisoformat(entry["at"]) >= cutoff
        ]
        expired_turns = select(AssistantTurn.id).where(
            AssistantTurn.conversation_id == conversation.id,
        )
        if not changed_household:
            expired_turns = expired_turns.where(AssistantTurn.created_at < cutoff)
        # Retain deduplication IDs and usage; expire private response/action payloads.
        await self.session.execute(
            update(AssistantAction)
            .where(
                AssistantAction.turn_id.in_(expired_turns),
            )
            .values(result={})
        )
        await self.session.execute(
            update(AssistantTurn)
            .where(
                AssistantTurn.id.in_(expired_turns),
            )
            .values(response="This earlier response has expired; the action will not be repeated.")
        )
        await self.session.commit()
        return conversation

    async def start_turn(self, conversation_id, message_key):
        await self.fence()
        existing = await self.session.scalar(
            select(AssistantTurn).where(
                AssistantTurn.conversation_id == conversation_id,
                AssistantTurn.message_key == message_key,
            )
        )
        if existing:
            return existing, False
        turn = AssistantTurn(conversation_id=conversation_id, message_key=message_key)
        self.session.add(turn)
        await self.session.commit()
        return turn, True

    async def finish(self, conversation, turn, user_text, response, evidence):
        await self.fence()
        conversation.history = [
            *conversation.history,
            {
                "user": user_text[:4000],
                "assistant": response[:4000],
                "evidence": evidence,
                "at": datetime.now(UTC).isoformat(),
            },
        ][-8:]
        turn.status = "complete"
        turn.response = response
        await self.session.commit()

    async def action(self, turn_id, action_key):
        return await self.session.scalar(
            select(AssistantAction).where(
                AssistantAction.turn_id == turn_id,
                AssistantAction.action_key == action_key,
            )
        )

    async def reserve(
        self, household_id, turn_id, model, route, tokens, limit, prompt_version, *, output_tokens=0
    ):
        await self.fence()
        policy = await self.session.get(AssistantHouseholdPolicy, household_id)
        if policy and policy.monthly_token_limit is not None:
            limit = policy.monthly_token_limit
        dollars = estimate(model, tokens, output_tokens, reserve=True)
        month = datetime.now(UTC).date().replace(day=1)
        await self.session.execute(
            insert(AssistantBudget)
            .values(
                household_id=household_id,
                month=month,
                tokens=0,
            )
            .on_conflict_do_nothing(constraint="uq_assistant_budget")
        )
        budget = await self.session.scalar(
            select(AssistantBudget)
            .where(
                AssistantBudget.household_id == household_id,
                AssistantBudget.month == month,
            )
            .with_for_update()
        )
        if budget.tokens + tokens > limit:
            await self.session.commit()
            raise BudgetExceeded
        if (
            policy
            and policy.monthly_usd_limit is not None
            and (dollars is None or budget.estimated_usd + dollars > policy.monthly_usd_limit)
        ):
            await self.session.commit()
            raise BudgetExceeded
        budget.tokens += tokens
        if dollars is not None:
            budget.estimated_usd += dollars
        call = AssistantModelCall(
            household_id=household_id,
            turn_id=turn_id,
            model=model,
            route=route,
            reserved_tokens=tokens,
            prompt_version=prompt_version,
            estimated_usd=dollars,
        )
        self.session.add(call)
        await self.session.commit()
        return call, month

    async def settle(self, call, month, usage, duration_ms, status):
        await self.fence()
        call.duration_ms = duration_ms
        call.status = status
        if usage is not None:
            call.input_tokens = usage.get("input_tokens", 0)
            call.output_tokens = usage.get("output_tokens", 0)
            call.cached_tokens = usage.get("input_tokens_details", {}).get("cached_tokens", 0)
            call.cache_write_tokens = usage.get("input_tokens_details", {}).get(
                "cache_write_tokens", 0
            )
            actual = call.input_tokens + call.output_tokens
            budget = await self.session.scalar(
                select(AssistantBudget)
                .where(
                    AssistantBudget.household_id == call.household_id,
                    AssistantBudget.month == month,
                )
                .with_for_update()
            )
            budget.tokens += actual - call.reserved_tokens
            dollars = estimate(
                call.model,
                call.input_tokens,
                call.output_tokens,
                cached=call.cached_tokens,
                writes=call.cache_write_tokens,
            )
            if dollars is not None and call.estimated_usd is not None:
                budget.estimated_usd += dollars - call.estimated_usd
                call.estimated_usd = dollars
        # Unknown provider outcome retains the reservation; retries are never free/unbounded.
        await self.session.commit()
