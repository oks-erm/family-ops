from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActivityAction, ActivityLog


class ActivityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def log(
        self,
        *,
        household_id: UUID,
        user_id: UUID | None,
        action: ActivityAction,
        entity_type: str,
        entity_id: UUID | None,
        summary: str,
        metadata: dict[str, object] | None = None,
        commit: bool = True,
    ) -> ActivityLog:
        entry = ActivityLog(
            household_id=household_id,
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
            metadata_json=metadata or {},
        )
        self.session.add(entry)
        if commit:
            await self.session.commit()
            await self.session.refresh(entry)
        return entry

    async def list_recent(self, *, household_id: UUID, limit: int = 30) -> list[ActivityLog]:
        result = await self.session.execute(
            select(ActivityLog)
            .where(ActivityLog.household_id == household_id)
            .order_by(ActivityLog.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
