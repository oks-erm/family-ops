import logging
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CalendarConnection,
    CalendarProvider,
    LessonBooking,
    LessonPaymentAllocation,
    LessonType,
    SchedulingCalendar,
    SchedulingProfile,
    StudentMeeting,
)
from app.db.repositories.scheduling import SchedulingRepository
from app.services.calendar_service import CalendarEventMatchError, CalendarService
from app.services.scheduling_rules import (
    BusyPeriod,
    SchedulingValidationError,
    generate_slots,
    normalize_slug,
    validate_timezone,
)


class SlotUnavailableError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


class SchedulingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = SchedulingRepository(session)

    async def ensure_profile(
        self, *, user_id: UUID, household_id: UUID, display_name: str, timezone: str
    ) -> SchedulingProfile:
        profile = await self.repository.profile_for_user(user_id=user_id)
        if profile is not None:
            return profile
        try:
            base = normalize_slug(display_name or "tutor")
        except SchedulingValidationError:
            base = "tutor"
        slug = base
        suffix = 2
        while await self.repository.profile_by_slug(slug=slug, active_only=False) is not None:
            slug = f"{base}-{suffix}"
            suffix += 1
        profile = await self.repository.create_profile(
            user_id=user_id,
            household_id=household_id,
            slug=slug,
            display_name=display_name or "Tutor",
            timezone=validate_timezone(timezone),
        )
        await self.repository.replace_rules(
            profile_id=profile.id,
            rules=[(weekday, time(9), time(17)) for weekday in range(5)],
        )
        lesson_type = LessonType(
            profile_id=profile.id,
            name="Lesson",
            duration_minutes=60,
            is_active=True,
        )
        self.session.add(lesson_type)
        await self.session.commit()
        return profile

    async def discover_google_calendars(
        self,
        *,
        profile: SchedulingProfile,
        connection_id: UUID | None = None,
    ) -> int:
        query = select(CalendarConnection).where(
            CalendarConnection.household_id == profile.household_id,
            CalendarConnection.provider == CalendarProvider.google,
        )
        if connection_id is not None:
            query = query.where(CalendarConnection.id == connection_id)
        result = await self.session.execute(query)
        connections = list(result.scalars().all())
        discovered = 0
        calendar_service = CalendarService(self.session)
        async with httpx.AsyncClient(timeout=12) as client:
            for connection in connections:
                access_token = await calendar_service.google_access_token(
                    client=client, connection=connection
                )
                if not access_token:
                    continue
                page_token: str | None = None
                while True:
                    params = {"maxResults": "250"}
                    if page_token:
                        params["pageToken"] = page_token
                    response = await client.get(
                        "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                        params=params,
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    for item in payload.get("items", []):
                        calendar_id = str(item.get("id") or "").strip()
                        if not calendar_id:
                            continue
                        existing_result = await self.session.execute(
                            select(SchedulingCalendar).where(
                                SchedulingCalendar.profile_id == profile.id,
                                SchedulingCalendar.connection_id == connection.id,
                                SchedulingCalendar.external_calendar_id == calendar_id,
                            )
                        )
                        source = existing_result.scalar_one_or_none()
                        access_role = str(item.get("accessRole") or "") or None
                        can_write = access_role in {"owner", "writer"}
                        if source is None:
                            source = SchedulingCalendar(
                                profile_id=profile.id,
                                connection_id=connection.id,
                                external_calendar_id=calendar_id,
                                name=str(
                                    item.get("summaryOverride")
                                    or item.get("summary")
                                    or calendar_id
                                ),
                                access_role=access_role,
                                include_in_conflicts=True,
                                can_write=can_write,
                            )
                            self.session.add(source)
                        else:
                            source.name = str(
                                item.get("summaryOverride") or item.get("summary") or calendar_id
                            )
                            source.access_role = access_role
                            source.can_write = can_write
                        if (
                            profile.booking_calendar_id is None
                            and item.get("primary")
                            and can_write
                        ):
                            profile.booking_calendar_id = calendar_id
                        discovered += 1
                    page_token = payload.get("nextPageToken")
                    if not page_token:
                        break
        await self.session.commit()
        return discovered

    async def slots(
        self,
        *,
        profile: SchedulingProfile,
        lesson_type: LessonType,
        start_day: date,
        end_day: date,
        now: datetime | None = None,
    ) -> list[datetime]:
        if end_day < start_day or (end_day - start_day).days > 31:
            raise SchedulingValidationError("Request between 1 and 32 days of availability.")
        now = now or datetime.now(UTC)
        tz = ZoneInfo(profile.timezone)
        earliest = now.astimezone(tz) + timedelta(minutes=profile.minimum_notice_minutes)
        latest = now.astimezone(tz) + timedelta(days=profile.booking_window_days)
        range_start = datetime.combine(start_day, time.min, tzinfo=tz)
        range_end = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=tz)
        conflict_range_start = range_start - timedelta(minutes=profile.buffer_before_minutes)
        conflict_range_end = range_end + timedelta(minutes=profile.buffer_after_minutes)
        calendars = await self.repository.list_calendars(profile_id=profile.id)
        configured_connection_ids = {calendar.connection_id for calendar in calendars}
        enabled_calendar_sources = {
            (calendar.connection_id, calendar.external_calendar_id)
            for calendar in calendars
            if calendar.include_in_conflicts
        }
        events = await self.repository.busy_events(
            household_id=profile.household_id,
            starts_before=conflict_range_end,
            ends_after=conflict_range_start,
        )
        bookings = await self.repository.bookings_between(
            profile_id=profile.id,
            starts_before=conflict_range_end,
            ends_after=conflict_range_start,
        )
        lesson_event_ids = {
            f"{booking.external_calendar_id}:{booking.external_event_id}"
            for booking in bookings
            if booking.external_calendar_id and booking.external_event_id
        }
        busy = []
        for event in events:
            if event.source_type in {CalendarProvider.google, CalendarProvider.icloud}:
                calendar_id = str((event.raw_event or {}).get("_calendar_id") or "")
                if (
                    event.source_id in configured_connection_ids
                    and (event.source_id, calendar_id) not in enabled_calendar_sources
                ):
                    continue
                if (
                    event.source_type == CalendarProvider.google
                    and event.external_event_id in lesson_event_ids
                ):
                    continue
            busy.append(BusyPeriod(event.starts_at, event.ends_at))
        busy.extend(
            BusyPeriod(item.starts_at, item.ends_at, requires_buffer=False)
            for item in bookings
        )
        rules = [
            (rule.weekday, rule.starts_at, rule.ends_at)
            for rule in await self.repository.list_rules(profile_id=profile.id)
        ]
        slots: list[datetime] = []
        day = start_day
        while day <= end_day:
            slots.extend(
                generate_slots(
                    day=day,
                    timezone=profile.timezone,
                    rules=rules,
                    duration_minutes=lesson_type.duration_minutes,
                    interval_minutes=profile.slot_interval_minutes,
                    buffer_before_minutes=profile.buffer_before_minutes,
                    buffer_after_minutes=profile.buffer_after_minutes,
                    earliest_start=earliest,
                    latest_start=latest,
                    busy_periods=busy,
                )
            )
            day += timedelta(days=1)
        return slots

    async def book(
        self,
        *,
        profile: SchedulingProfile,
        lesson_type: LessonType,
        starts_at: datetime,
        student_name: str,
        student_email: str,
        student_timezone: str,
        notes: str | None,
        guest_booking: bool = False,
    ) -> LessonBooking:
        bookings = await self.book_many(
            profile=profile,
            lesson_type=lesson_type,
            starts_at=[starts_at],
            student_name=student_name,
            student_email=student_email,
            student_timezone=student_timezone,
            notes=notes,
            guest_booking=guest_booking,
        )
        return bookings[0]

    async def book_many(
        self,
        *,
        profile: SchedulingProfile,
        lesson_type: LessonType,
        starts_at: list[datetime],
        student_name: str,
        student_email: str,
        student_timezone: str,
        notes: str | None,
        guest_booking: bool = False,
    ) -> list[LessonBooking]:
        validate_timezone(student_timezone)
        if not 1 <= len(starts_at) <= 10:
            raise SchedulingValidationError("Choose between 1 and 10 lesson times.")
        if len(set(starts_at)) != len(starts_at):
            raise SchedulingValidationError("Each lesson time must be unique.")
        if any(value.tzinfo is None for value in starts_at):
            raise SchedulingValidationError("Every selected lesson time must include a timezone.")
        starts_at = sorted(starts_at)
        duration = timedelta(minutes=lesson_type.duration_minutes)
        for previous, current in zip(starts_at, starts_at[1:], strict=False):
            if current < previous + duration:
                raise SchedulingValidationError("Selected lesson times cannot overlap.")
        recent_count = await self.repository.recent_booking_count(
            profile_id=profile.id,
            student_email=student_email,
            created_after=datetime.now(UTC) - timedelta(hours=1),
        )
        if recent_count + len(starts_at) > 20:
            raise SchedulingValidationError(
                "Too many lessons were booked with this email recently. Try again later."
            )
        # Refresh immediately before booking, in addition to the five-minute background sync.
        # Only selected scheduling sources participate: an obsolete connection with no enabled
        # calendars must not prevent bookings, while any selected source still fails closed.
        calendar_service = await self._refresh_booking_calendars(profile=profile)
        await self.repository.lock_profile(profile_id=profile.id)
        for requested_start in starts_at:
            local_start = requested_start.astimezone(ZoneInfo(profile.timezone))
            available = await self.slots(
                profile=profile,
                lesson_type=lesson_type,
                start_day=local_start.date(),
                end_day=local_start.date(),
            )
            if not any(slot == local_start for slot in available):
                await self.session.rollback()
                raise SlotUnavailableError(
                    "One of those lesson times is no longer available. Choose another time."
                )
        title = f"{lesson_type.name} — {student_name.strip()}"
        normalized_student_email = student_email.strip().casefold()
        student_meeting = None
        if not guest_booking:
            student_meeting = await self.repository.student_meeting(
                profile_id=profile.id,
                student_email=normalized_student_email,
            )
        conference_data = student_meeting.conference_data if student_meeting else None
        created_events: list[tuple[str | None, str]] = []
        bookings: list[LessonBooking] = []
        try:
            for requested_start in starts_at:
                ends_at = requested_start + duration
                event_conference_data = None if guest_booking else conference_data
                calendar_event = await calendar_service.create_google_event(
                    household_id=profile.household_id,
                    user_id=profile.user_id,
                    title=title,
                    starts_at=requested_start,
                    ends_at=ends_at,
                    timezone=profile.timezone,
                    location=lesson_type.location,
                    calendar_id_override=profile.booking_calendar_id,
                    description=(
                        f"Student: {student_name.strip()}\nEmail: {normalized_student_email}"
                    ),
                    attendee_email=normalized_student_email,
                    conference_data=event_conference_data,
                    create_google_meet=event_conference_data is None,
                    commit_cache=False,
                )
                if calendar_event.meeting_url is None or calendar_event.conference_data is None:
                    raise CalendarEventMatchError(
                        "Google did not attach a Meet conference to the lesson."
                    )
                if not calendar_event.external_event_id:
                    raise CalendarEventMatchError("Google did not return an event ID.")
                created_events.append(
                    (calendar_event.external_calendar_id, calendar_event.external_event_id)
                )
                if not guest_booking:
                    conference_data = calendar_event.conference_data
                if not guest_booking and student_meeting is None:
                    student_meeting = StudentMeeting(
                        profile_id=profile.id,
                        student_email=normalized_student_email,
                        meeting_url=calendar_event.meeting_url,
                        conference_data=conference_data,
                    )
                    await self.repository.add_student_meeting(student_meeting)
                booking = LessonBooking(
                    profile_id=profile.id,
                    lesson_type_id=lesson_type.id,
                    student_name=student_name.strip(),
                    student_email=normalized_student_email,
                    student_timezone=student_timezone,
                    notes=(notes or "").strip() or None,
                    starts_at=requested_start,
                    ends_at=ends_at,
                    status="confirmed",
                    external_calendar_id=calendar_event.external_calendar_id,
                    external_event_id=calendar_event.external_event_id,
                    meeting_url=calendar_event.meeting_url,
                )
                await self.repository.add_booking(booking)
                bookings.append(booking)
            if not guest_booking:
                await self._allocate_available_credits(
                    profile_id=profile.id,
                    student_email=normalized_student_email,
                    bookings=bookings,
                )
            await self.session.commit()
            for booking in bookings:
                await self.session.refresh(booking)
            return bookings
        except Exception:
            await self.session.rollback()
            for calendar_id, event_id in reversed(created_events):
                try:
                    await calendar_service.delete_google_event_by_id(
                        household_id=profile.household_id,
                        calendar_id=calendar_id,
                        event_id=event_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to remove Google event during batch booking rollback",
                        extra={"calendar_id": calendar_id, "event_id": event_id},
                    )
            raise

    async def _allocate_available_credits(
        self,
        *,
        profile_id: UUID,
        student_email: str,
        bookings: list[LessonBooking],
    ) -> None:
        payments = await self.repository.student_payments(
            profile_id=profile_id,
            student_email=student_email,
        )
        all_allocated_ids = await self.repository.allocation_booking_ids(
            payment_ids=[payment.id for payment in payments]
        )
        for payment in payments:
            allocated_ids = await self.repository.allocation_booking_ids(
                payment_ids=[payment.id]
            )
            remaining = payment.lessons_purchased - len(allocated_ids)
            if remaining <= 0:
                continue
            for booking in bookings:
                if remaining <= 0:
                    break
                if booking.id in all_allocated_ids:
                    continue
                if payment.valid_from is None:
                    payment.valid_from = booking.starts_at
                    payment.expires_at = booking.starts_at + timedelta(weeks=5)
                if payment.expires_at is not None and booking.starts_at > payment.expires_at:
                    continue
                self.session.add(
                    LessonPaymentAllocation(payment_id=payment.id, booking_id=booking.id)
                )
                all_allocated_ids.add(booking.id)
                remaining -= 1

    async def _refresh_booking_calendars(
        self, *, profile: SchedulingProfile
    ) -> CalendarService:
        calendar_service = CalendarService(self.session)
        await calendar_service.sync_ical_feeds(household_id=profile.household_id)
        configured_calendars = await self.repository.list_calendars(profile_id=profile.id)
        connection_ids = {
            calendar.connection_id
            for calendar in configured_calendars
            if calendar.include_in_conflicts
        }
        for connection_id in connection_ids:
            connection = await self.session.get(CalendarConnection, connection_id)
            if connection is None or connection.household_id != profile.household_id:
                continue
            if connection.provider == CalendarProvider.google:
                await calendar_service.sync_google_connections(
                    household_id=profile.household_id,
                    connection_id=connection.id,
                )
            elif connection.provider == CalendarProvider.icloud:
                await calendar_service.sync_icloud_connections(
                    household_id=profile.household_id,
                    connection_id=connection.id,
                )
        return calendar_service
