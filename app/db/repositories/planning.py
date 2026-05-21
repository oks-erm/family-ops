from datetime import date, time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DailyPlan, DailyPlanStatus, PlanningConversation, PlanningConversationState


class PlanningRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def start_conversation(
        self,
        *,
        user_id: UUID,
        household_id: UUID,
        plan_date: date,
    ) -> PlanningConversation:
        stmt = (
            insert(PlanningConversation)
            .values(
                user_id=user_id,
                household_id=household_id,
                plan_date=plan_date,
                state=PlanningConversationState.awaiting_work_start,
                raw_notes=[],
            )
            .on_conflict_do_update(
                constraint="uq_planning_conversations_user_date",
                set_={
                    "household_id": household_id,
                    "state": PlanningConversationState.awaiting_work_start,
                    "work_start": None,
                    "work_end": None,
                    "unusual_notes": None,
                    "raw_notes": [],
                },
            )
            .returning(PlanningConversation)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one()

    async def get_active_conversation(self, *, user_id: UUID) -> PlanningConversation | None:
        result = await self.session.execute(
            select(PlanningConversation)
            .where(
                PlanningConversation.user_id == user_id,
                PlanningConversation.state != PlanningConversationState.complete,
            )
            .order_by(PlanningConversation.created_at.desc())
        )
        return result.scalars().first()

    async def get_conversation(
        self,
        *,
        user_id: UUID,
        plan_date: date,
    ) -> PlanningConversation | None:
        result = await self.session.execute(
            select(PlanningConversation).where(
                PlanningConversation.user_id == user_id,
                PlanningConversation.plan_date == plan_date,
            )
        )
        return result.scalar_one_or_none()

    async def save_answer(
        self,
        *,
        conversation: PlanningConversation,
        message_text: str,
        work_start: time | None = None,
        work_end: time | None = None,
        unusual_notes: str | None = None,
        next_state: PlanningConversationState,
    ) -> PlanningConversation:
        notes = list(conversation.raw_notes or [])
        notes.append({"state": conversation.state.value, "text": message_text})
        if work_start is not None:
            conversation.work_start = work_start
        if work_end is not None:
            conversation.work_end = work_end
        if unusual_notes is not None:
            conversation.unusual_notes = unusual_notes
        conversation.state = next_state
        conversation.raw_notes = notes
        await self.session.commit()
        await self.session.refresh(conversation)
        return conversation

    async def upsert_daily_plan(
        self,
        *,
        user_id: UUID,
        household_id: UUID,
        plan_date: date,
        work_start: time | None,
        work_end: time | None,
        unusual_notes: str | None,
        plan: dict[str, object],
        status: DailyPlanStatus,
    ) -> DailyPlan:
        stmt = (
            insert(DailyPlan)
            .values(
                user_id=user_id,
                household_id=household_id,
                plan_date=plan_date,
                work_start=work_start,
                work_end=work_end,
                unusual_notes=unusual_notes,
                plan=plan,
                status=status,
            )
            .on_conflict_do_update(
                constraint="uq_daily_plans_user_date",
                set_={
                    "household_id": household_id,
                    "work_start": work_start,
                    "work_end": work_end,
                    "unusual_notes": unusual_notes,
                    "plan": plan,
                    "status": status,
                },
            )
            .returning(DailyPlan)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one()

    async def get_daily_plan(self, *, user_id: UUID, plan_date: date) -> DailyPlan | None:
        result = await self.session.execute(
            select(DailyPlan).where(DailyPlan.user_id == user_id, DailyPlan.plan_date == plan_date)
        )
        return result.scalar_one_or_none()
