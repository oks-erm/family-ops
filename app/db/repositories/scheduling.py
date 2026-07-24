from datetime import datetime, time
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AvailabilityRule,
    CalendarEventCache,
    ICalFeed,
    LessonBooking,
    LessonType,
    SchedulingCalendar,
    SchedulingProfile,
)


class SchedulingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def profile_for_user(self, *, user_id: UUID) -> SchedulingProfile | None:
        result = await self.session.execute(
            select(SchedulingProfile).where(SchedulingProfile.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def profile_by_slug(
        self, *, slug: str, active_only: bool = True
    ) -> SchedulingProfile | None:
        query = select(SchedulingProfile).where(SchedulingProfile.slug == slug)
        if active_only:
            query = query.where(SchedulingProfile.is_active.is_(True))
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def create_profile(
        self,
        *,
        user_id: UUID,
        household_id: UUID,
        slug: str,
        display_name: str,
        timezone: str,
    ) -> SchedulingProfile:
        profile = SchedulingProfile(
            user_id=user_id,
            household_id=household_id,
            slug=slug,
            display_name=display_name,
            timezone=timezone,
        )
        self.session.add(profile)
        await self.session.commit()
        await self.session.refresh(profile)
        return profile

    async def list_lesson_types(
        self, *, profile_id: UUID, active_only: bool = False
    ) -> list[LessonType]:
        query = select(LessonType).where(LessonType.profile_id == profile_id)
        if active_only:
            query = query.where(LessonType.is_active.is_(True))
        result = await self.session.execute(
            query.order_by(LessonType.duration_minutes, LessonType.name)
        )
        return list(result.scalars().all())

    async def lesson_type(self, *, profile_id: UUID, lesson_type_id: UUID) -> LessonType | None:
        result = await self.session.execute(
            select(LessonType).where(
                LessonType.id == lesson_type_id,
                LessonType.profile_id == profile_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_rules(self, *, profile_id: UUID) -> list[AvailabilityRule]:
        result = await self.session.execute(
            select(AvailabilityRule)
            .where(AvailabilityRule.profile_id == profile_id, AvailabilityRule.is_active.is_(True))
            .order_by(AvailabilityRule.weekday, AvailabilityRule.starts_at)
        )
        return list(result.scalars().all())

    async def replace_rules(
        self, *, profile_id: UUID, rules: list[tuple[int, time, time]]
    ) -> list[AvailabilityRule]:
        await self.session.execute(
            delete(AvailabilityRule).where(AvailabilityRule.profile_id == profile_id)
        )
        created = [
            AvailabilityRule(
                profile_id=profile_id,
                weekday=weekday,
                starts_at=starts_at,
                ends_at=ends_at,
                is_active=True,
            )
            for weekday, starts_at, ends_at in rules
        ]
        self.session.add_all(created)
        await self.session.commit()
        return created

    async def list_calendars(self, *, profile_id: UUID) -> list[SchedulingCalendar]:
        result = await self.session.execute(
            select(SchedulingCalendar)
            .where(SchedulingCalendar.profile_id == profile_id)
            .order_by(SchedulingCalendar.name)
        )
        return list(result.scalars().all())

    async def enabled_calendars(self, *, profile_id: UUID) -> list[SchedulingCalendar]:
        result = await self.session.execute(
            select(SchedulingCalendar).where(
                SchedulingCalendar.profile_id == profile_id,
                SchedulingCalendar.include_in_conflicts.is_(True),
            )
        )
        return list(result.scalars().all())

    async def list_ical_feeds(self, *, household_id: UUID) -> list[ICalFeed]:
        result = await self.session.execute(
            select(ICalFeed)
            .where(ICalFeed.household_id == household_id)
            .order_by(ICalFeed.created_at)
        )
        return list(result.scalars().all())

    async def busy_events(
        self, *, household_id: UUID, starts_before: datetime, ends_after: datetime
    ) -> list[CalendarEventCache]:
        result = await self.session.execute(
            select(CalendarEventCache).where(
                CalendarEventCache.household_id == household_id,
                CalendarEventCache.starts_at < starts_before,
                CalendarEventCache.ends_at > ends_after,
            )
        )
        return list(result.scalars().all())

    async def bookings_between(
        self, *, profile_id: UUID, starts_before: datetime, ends_after: datetime
    ) -> list[LessonBooking]:
        result = await self.session.execute(
            select(LessonBooking)
            .where(
                LessonBooking.profile_id == profile_id,
                LessonBooking.status == "confirmed",
                LessonBooking.starts_at < starts_before,
                LessonBooking.ends_at > ends_after,
            )
            .order_by(LessonBooking.starts_at)
        )
        return list(result.scalars().all())

    async def upcoming_bookings(self, *, profile_id: UUID, now: datetime) -> list[LessonBooking]:
        result = await self.session.execute(
            select(LessonBooking)
            .where(
                LessonBooking.profile_id == profile_id,
                LessonBooking.status == "confirmed",
                LessonBooking.ends_at >= now,
            )
            .order_by(LessonBooking.starts_at)
            .limit(200)
        )
        return list(result.scalars().all())

    async def lock_profile(self, *, profile_id: UUID) -> None:
        # Serialize bookings for one tutor so two simultaneous requests cannot take one slot.
        await self.session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:profile_id, 0))"),
            {"profile_id": str(profile_id)},
        )

    async def recent_booking_count(
        self,
        *,
        profile_id: UUID,
        student_email: str,
        created_after: datetime,
    ) -> int:
        result = await self.session.execute(
            select(func.count(LessonBooking.id)).where(
                LessonBooking.profile_id == profile_id,
                LessonBooking.student_email == student_email.strip().casefold(),
                LessonBooking.created_at >= created_after,
            )
        )
        return int(result.scalar_one())

    async def add_booking(self, booking: LessonBooking) -> LessonBooking:
        self.session.add(booking)
        await self.session.flush()
        return booking
