from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import HouseholdRecommendation


class RecommendationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def latest_for_household(self, *, household_id: UUID) -> HouseholdRecommendation | None:
        result = await self.session.execute(
            select(HouseholdRecommendation)
            .where(HouseholdRecommendation.household_id == household_id)
            .order_by(HouseholdRecommendation.period_end.desc(), HouseholdRecommendation.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def upsert_weekly_recommendation(
        self,
        *,
        household_id: UUID,
        period_start: date,
        period_end: date,
        recommendations: list[str],
        metrics: dict[str, object],
    ) -> HouseholdRecommendation:
        stmt = (
            insert(HouseholdRecommendation)
            .values(
                household_id=household_id,
                period_start=period_start,
                period_end=period_end,
                recommendations=recommendations,
                metrics=metrics,
            )
            .on_conflict_do_update(
                constraint="uq_household_recommendation_period",
                set_={
                    "recommendations": recommendations,
                    "metrics": metrics,
                },
            )
            .returning(HouseholdRecommendation)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one()
