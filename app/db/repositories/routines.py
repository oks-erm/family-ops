from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Routine


class RoutineRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_active_for_household(self, *, household_id: UUID) -> list[Routine]:
        result = await self.session.execute(
            select(Routine)
            .where(Routine.household_id == household_id, Routine.is_active.is_(True))
            .order_by(Routine.created_at)
        )
        return list(result.scalars().all())

    async def list_for_household(self, *, household_id: UUID) -> list[Routine]:
        result = await self.session.execute(
            select(Routine)
            .where(Routine.household_id == household_id)
            .order_by(Routine.is_active.desc(), Routine.created_at)
        )
        return list(result.scalars().all())

    async def ensure_defaults(self, *, household_id: UUID) -> None:
        existing = await self.list_for_household(household_id=household_id)
        existing_by_title = {routine.title.strip().casefold(): routine for routine in existing}
        defaults = [
            ("read the bible", 15, 15),
            ("exercise", 15, 30),
        ]
        changed = False
        for title, duration_min, duration_max in defaults:
            existing_routine = existing_by_title.get(title)
            if existing_routine is not None:
                schedule = dict(existing_routine.schedule or {})
                if (
                    schedule.get("frequency") != "daily"
                    or schedule.get("must") is not True
                    or "duration_minutes" not in schedule
                    or "duration_min" not in schedule
                    or "duration_max" not in schedule
                ):
                    schedule["frequency"] = "daily"
                    schedule["must"] = True
                    if "duration_min" not in schedule or "duration_max" not in schedule:
                        schedule["duration_minutes"] = duration_min
                    schedule.setdefault("duration_minutes", duration_min)
                    schedule.setdefault("duration_min", duration_min)
                    schedule.setdefault("duration_max", duration_max)
                    existing_routine.schedule = schedule
                    changed = True
                continue
            self.session.add(
                Routine(
                    household_id=household_id,
                    title=title,
                    schedule={
                        "frequency": "daily",
                        "must": True,
                        "duration_minutes": duration_min,
                        "duration_min": duration_min,
                        "duration_max": duration_max,
                    },
                    is_active=True,
                )
            )
            changed = True
        if changed:
            await self.session.commit()

    async def create(
        self,
        *,
        household_id: UUID,
        title: str,
        duration_minutes: int,
        duration_max: int | None = None,
    ) -> Routine:
        routine = Routine(
            household_id=household_id,
            title=title.strip(),
            schedule={
                "frequency": "daily",
                "must": True,
                "duration_minutes": duration_minutes,
                "duration_min": duration_minutes,
                "duration_max": duration_max or duration_minutes,
            },
            is_active=True,
        )
        self.session.add(routine)
        await self.session.commit()
        await self.session.refresh(routine)
        return routine

    async def update(
        self,
        *,
        routine: Routine,
        title: str | None = None,
        duration_minutes: int | None = None,
        duration_max: int | None = None,
        is_active: bool | None = None,
    ) -> Routine:
        if title is not None:
            routine.title = title.strip()
        if duration_minutes is not None:
            schedule = dict(routine.schedule or {})
            schedule["frequency"] = schedule.get("frequency") or "daily"
            schedule["must"] = True
            schedule["duration_minutes"] = duration_minutes
            schedule["duration_min"] = duration_minutes
            schedule["duration_max"] = duration_max or duration_minutes
            routine.schedule = schedule
        if is_active is not None:
            routine.is_active = is_active
        await self.session.commit()
        await self.session.refresh(routine)
        return routine

    async def find_by_title(self, *, household_id: UUID, title: str) -> Routine | None:
        normalized = title.strip().casefold()
        routines = await self.list_for_household(household_id=household_id)
        for routine in routines:
            routine_title = routine.title.strip().casefold()
            if routine_title == normalized or normalized in routine_title or routine_title in normalized:
                return routine
        return None

    async def delete(self, *, routine: Routine) -> None:
        await self.session.delete(routine)
        await self.session.commit()
