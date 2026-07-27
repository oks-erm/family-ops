import html
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    CalendarConnection,
    CalendarEventCache,
    CalendarProvider,
    ICalFeed,
    LessonBooking,
    LessonPaymentAllocation,
    LessonType,
    SchedulingCalendar,
    StudentPayment,
)
from app.db.repositories.calendar import CalendarRepository
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.scheduling import SchedulingRepository
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory
from app.services.calendar_service import (
    CalDAVProtocolError,
    CalendarEventMatchError,
    CalendarNotConnectedError,
    CalendarService,
    CalendarSyncError,
    CalendarWritePermissionError,
    ICloudAuthenticationError,
)
from app.services.credential_cipher import CredentialCipher
from app.services.scheduling_rules import (
    SchedulingValidationError,
    normalize_slug,
    validate_timezone,
)
from app.services.scheduling_service import (
    SchedulingService,
    SlotUnavailableError,
)
from app.utils.urls import UnsafeExternalURLError, validate_public_https_url

router = APIRouter(tags=["scheduling"])


class ProfileRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=3, max_length=100)
    timezone: str = Field(min_length=1, max_length=64)
    minimum_notice_minutes: int = Field(ge=0, le=43200)
    booking_window_days: int = Field(ge=1, le=365)
    buffer_before_minutes: int = Field(ge=0, le=240)
    buffer_after_minutes: int = Field(ge=0, le=240)
    slot_interval_minutes: int = Field(ge=5, le=120)
    booking_calendar_id: str | None = Field(default=None, max_length=255)
    is_active: bool = True


class LessonTypeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    duration_minutes: int = Field(ge=15, le=480)
    location: str | None = Field(default=None, max_length=500)
    is_active: bool = True


class AvailabilityItem(BaseModel):
    weekday: int = Field(ge=0, le=6)
    starts_at: time
    ends_at: time


class AvailabilityRequest(BaseModel):
    rules: list[AvailabilityItem] = Field(max_length=50)


class CalendarSelectionRequest(BaseModel):
    include_in_conflicts: bool


class ICalRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=8, max_length=4000)


class ICloudConnectionRequest(BaseModel):
    account_email: str = Field(min_length=3, max_length=320)
    app_specific_password: SecretStr = Field(min_length=8, max_length=128)


class BookingRequest(BaseModel):
    lesson_type_id: UUID
    starts_at: list[datetime] = Field(min_length=1, max_length=10)
    student_timezone: str = Field(min_length=1, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)
    website: str | None = Field(default=None, max_length=200)  # Honeypot.


class StudentPaymentRequest(BaseModel):
    student_email: str = Field(min_length=3, max_length=320)
    lessons_purchased: int = Field(ge=1, le=100)
    amount_cents: int | None = Field(default=None, ge=0, le=10_000_000)
    booking_ids: list[UUID] = Field(default_factory=list, max_length=100)


async def _dashboard_context(request: Request, session):
    email = request.session.get("google_email")
    if not email:
        return None, None
    user = await UserRepository(session).get_by_google_email(google_email=str(email))
    if user is None:
        request.session.clear()
        return None, None
    household = await HouseholdRepository(session).ensure_household_for_user(user=user)
    return user, household


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    origin_parts = urlsplit(origin)
    request_host = request.headers.get("host", "").casefold()
    if (
        origin_parts.scheme not in {"http", "https"}
        or origin_parts.netloc.casefold() != request_host
    ):
        raise HTTPException(status_code=403, detail="Cross-origin management request rejected.")


def _email_is_valid(value: str) -> bool:
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value.strip()))


def _student_identity(request: Request) -> tuple[str, str]:
    email = str(request.session.get("student_google_email") or "").strip().casefold()
    if not _email_is_valid(email):
        raise HTTPException(status_code=401, detail="Google sign-in required.")
    name = str(request.session.get("student_google_name") or "").strip() or email.split("@", 1)[0]
    return email, name[:255]


async def _management_profile(request: Request, session):
    user, household = await _dashboard_context(request, session)
    if user is None or household is None:
        raise HTTPException(status_code=401, detail="Dashboard login required.")
    display_name = " ".join(part for part in [user.first_name, user.last_name] if part) or "Tutor"
    profile = await SchedulingService(session).ensure_profile(
        user_id=user.id,
        household_id=household.id,
        display_name=display_name,
        timezone=user.timezone,
    )
    return user, household, profile


def _lesson_type_json(item: LessonType) -> dict[str, object]:
    return {
        "id": str(item.id),
        "name": item.name,
        "description": item.description,
        "duration_minutes": item.duration_minutes,
        "location": item.location,
        "is_active": item.is_active,
    }


async def _remove_booking_calendar_event(
    *, session: AsyncSession, household_id: UUID, booking: LessonBooking
) -> None:
    if not booking.external_event_id:
        return
    try:
        await CalendarService(session).delete_google_event_by_id(
            household_id=household_id,
            calendar_id=booking.external_calendar_id,
            event_id=booking.external_event_id,
        )
    except (
        CalendarNotConnectedError,
        CalendarSyncError,
        CalendarWritePermissionError,
        httpx.HTTPError,
    ) as exc:
        raise HTTPException(
            status_code=503,
            detail="The calendar event could not be removed, so the lesson was not cancelled.",
        ) from exc
    booking.external_event_id = None


@router.get("/schedule/manage", response_class=HTMLResponse)
async def scheduling_management_page(request: Request):
    if not request.session.get("google_email"):
        return RedirectResponse("/auth/google/start?next=scheduling", status_code=303)
    return HTMLResponse(MANAGEMENT_HTML)


@router.get("/api/scheduling/manage")
async def scheduling_management_data(request: Request) -> dict[str, object]:
    async with async_session_factory() as session:
        _, household, profile = await _management_profile(request, session)
        repository = SchedulingRepository(session)
        google_connection_items = await CalendarRepository(session).list_google_connections(
            household_id=household.id
        )
        icloud_connection_items = await CalendarRepository(session).list_icloud_connections(
            household_id=household.id
        )
        connection_items = [*google_connection_items, *icloud_connection_items]
        connections = {item.id: item for item in connection_items}
        calendars = await repository.list_calendars(profile_id=profile.id)
        booking_result = await session.execute(
            select(LessonBooking)
            .where(LessonBooking.profile_id == profile.id)
            .order_by(LessonBooking.starts_at)
        )
        all_bookings = list(booking_result.scalars().all())
        payment_result = await session.execute(
            select(StudentPayment)
            .where(StudentPayment.profile_id == profile.id)
            .order_by(StudentPayment.paid_at)
        )
        all_payments = list(payment_result.scalars().all())
        payment_ids = [item.id for item in all_payments]
        allocation_result = await session.execute(
            select(LessonPaymentAllocation).where(
                LessonPaymentAllocation.payment_id.in_(payment_ids)
            )
        )
        allocations = {
            item.booking_id: item.payment_id for item in allocation_result.scalars().all()
        }
        student_emails = sorted(
            {item.student_email for item in all_bookings}
            | {item.student_email for item in all_payments}
        )
        settings = get_settings()
        public_base = (settings.scheduling_public_base_url or settings.public_base_url).rstrip("/")
        return {
            "profile": {
                "display_name": profile.display_name,
                "slug": profile.slug,
                "timezone": profile.timezone,
                "minimum_notice_minutes": profile.minimum_notice_minutes,
                "booking_window_days": profile.booking_window_days,
                "buffer_before_minutes": profile.buffer_before_minutes,
                "buffer_after_minutes": profile.buffer_after_minutes,
                "slot_interval_minutes": profile.slot_interval_minutes,
                "booking_calendar_id": profile.booking_calendar_id,
                "is_active": profile.is_active,
                "public_url": f"{public_base}/book/{profile.slug}",
            },
            "lesson_types": [
                _lesson_type_json(item)
                for item in await repository.list_lesson_types(profile_id=profile.id)
            ],
            "availability": [
                {
                    "weekday": item.weekday,
                    "starts_at": item.starts_at.strftime("%H:%M"),
                    "ends_at": item.ends_at.strftime("%H:%M"),
                }
                for item in await repository.list_rules(profile_id=profile.id)
            ],
            "google_accounts": [
                {
                    "id": str(item.id),
                    "account_email": item.account_email,
                    "calendar_count": sum(
                        calendar.connection_id == item.id for calendar in calendars
                    ),
                }
                for item in google_connection_items
            ],
            "icloud_accounts": [
                {
                    "id": str(item.id),
                    "account_email": item.account_email,
                    "calendar_count": sum(
                        calendar.connection_id == item.id for calendar in calendars
                    ),
                }
                for item in icloud_connection_items
            ],
            "calendars": [
                {
                    "id": str(item.id),
                    "external_calendar_id": item.external_calendar_id,
                    "name": item.name,
                    "account_email": (
                        connections[item.connection_id].account_email
                        if item.connection_id in connections
                        else None
                    ),
                    "provider": (
                        connections[item.connection_id].provider.value
                        if item.connection_id in connections
                        else None
                    ),
                    "access_role": item.access_role,
                    "include_in_conflicts": item.include_in_conflicts,
                    "can_write": item.can_write,
                }
                for item in calendars
            ],
            "ical_feeds": [
                {"id": str(item.id), "name": item.name, "is_active": item.is_active}
                for item in await repository.list_ical_feeds(household_id=household.id)
            ],
            "bookings": [
                {
                    "id": str(item.id),
                    "student_name": item.student_name,
                    "student_email": item.student_email,
                    "starts_at": item.starts_at.isoformat(),
                    "ends_at": item.ends_at.isoformat(),
                    "status": item.status,
                }
                for item in await repository.upcoming_bookings(
                    profile_id=profile.id, now=datetime.now(UTC)
                )
            ],
            "students": [
                {
                    "email": student_email,
                    "name": next(
                        (
                            item.student_name
                            for item in reversed(all_bookings)
                            if item.student_email == student_email
                        ),
                        student_email,
                    ),
                    "purchased": sum(
                        item.lessons_purchased
                        for item in all_payments
                        if item.student_email == student_email
                    ),
                    "allocated": sum(
                        item.id in allocations
                        for item in all_bookings
                        if item.student_email == student_email
                    ),
                    "bookings": [
                        {
                            "id": str(item.id),
                            "starts_at": item.starts_at.isoformat(),
                            "status": item.status,
                            "cancellation_consumes_credit": item.cancellation_consumes_credit,
                            "paid": item.id in allocations,
                        }
                        for item in all_bookings
                        if item.student_email == student_email
                    ],
                }
                for student_email in student_emails
            ],
        }


@router.put("/api/scheduling/profile")
async def update_scheduling_profile(request: Request, payload: ProfileRequest) -> dict[str, object]:
    _require_same_origin(request)
    try:
        slug = normalize_slug(payload.slug)
        timezone = validate_timezone(payload.timezone)
    except SchedulingValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    async with async_session_factory() as session:
        _, _, profile = await _management_profile(request, session)
        profile.display_name = payload.display_name.strip()
        profile.slug = slug
        profile.timezone = timezone
        profile.minimum_notice_minutes = payload.minimum_notice_minutes
        profile.booking_window_days = payload.booking_window_days
        profile.buffer_before_minutes = payload.buffer_before_minutes
        profile.buffer_after_minutes = payload.buffer_after_minutes
        profile.slot_interval_minutes = payload.slot_interval_minutes
        profile.booking_calendar_id = (payload.booking_calendar_id or "").strip() or None
        profile.is_active = payload.is_active
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=409, detail="That booking link is already in use."
            ) from exc
        return {"saved": True}


@router.post("/api/scheduling/lesson-types")
async def create_lesson_type(request: Request, payload: LessonTypeRequest) -> dict[str, object]:
    _require_same_origin(request)
    async with async_session_factory() as session:
        _, _, profile = await _management_profile(request, session)
        item = LessonType(
            profile_id=profile.id,
            name=payload.name.strip(),
            description=(payload.description or "").strip() or None,
            duration_minutes=payload.duration_minutes,
            location=(payload.location or "").strip() or None,
            is_active=payload.is_active,
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return _lesson_type_json(item)


@router.put("/api/scheduling/lesson-types/{lesson_type_id}")
async def update_lesson_type(
    request: Request, lesson_type_id: UUID, payload: LessonTypeRequest
) -> dict[str, object]:
    _require_same_origin(request)
    async with async_session_factory() as session:
        _, _, profile = await _management_profile(request, session)
        repository = SchedulingRepository(session)
        item = await repository.lesson_type(profile_id=profile.id, lesson_type_id=lesson_type_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Lesson type not found.")
        item.name = payload.name.strip()
        item.description = (payload.description or "").strip() or None
        item.duration_minutes = payload.duration_minutes
        item.location = (payload.location or "").strip() or None
        item.is_active = payload.is_active
        await session.commit()
        return _lesson_type_json(item)


@router.put("/api/scheduling/availability")
async def replace_availability(request: Request, payload: AvailabilityRequest) -> dict[str, object]:
    _require_same_origin(request)
    rules = []
    for item in payload.rules:
        if item.ends_at <= item.starts_at:
            raise HTTPException(status_code=400, detail="Availability end must be after its start.")
        rules.append((item.weekday, item.starts_at, item.ends_at))
    async with async_session_factory() as session:
        _, _, profile = await _management_profile(request, session)
        await SchedulingRepository(session).replace_rules(profile_id=profile.id, rules=rules)
        return {"saved": True, "count": len(rules)}


@router.post("/api/scheduling/bookings/{booking_id}/cancel")
async def cancel_booking(request: Request, booking_id: UUID) -> dict[str, bool]:
    _require_same_origin(request)
    async with async_session_factory() as session:
        _, _, profile = await _management_profile(request, session)
        booking = await session.get(LessonBooking, booking_id)
        if booking is None or booking.profile_id != profile.id:
            raise HTTPException(status_code=404, detail="Lesson booking not found.")
        await _remove_booking_calendar_event(
            session=session,
            household_id=profile.household_id,
            booking=booking,
        )
        if booking.status == "cancelled":
            await session.commit()
            return {"cancelled": True}
        booking.status = "cancelled"
        booking.cancelled_at = datetime.now(UTC)
        booking.cancellation_consumes_credit = False
        allocation = await SchedulingRepository(session).allocation_for_booking(
            booking_id=booking.id
        )
        if allocation is not None:
            await session.delete(allocation)
        await session.commit()
        return {"cancelled": True}


@router.post("/api/scheduling/student-payments", status_code=201)
async def add_student_payment(
    request: Request, payload: StudentPaymentRequest
) -> dict[str, object]:
    _require_same_origin(request)
    email = payload.student_email.strip().casefold()
    if not _email_is_valid(email):
        raise HTTPException(status_code=400, detail="Enter a valid student email address.")
    if len(set(payload.booking_ids)) != len(payload.booking_ids):
        raise HTTPException(status_code=400, detail="Select each lesson only once.")
    if len(payload.booking_ids) > payload.lessons_purchased:
        raise HTTPException(
            status_code=400,
            detail="A payment cannot be assigned to more lessons than it purchased.",
        )
    async with async_session_factory() as session:
        _, _, profile = await _management_profile(request, session)
        bookings: list[LessonBooking] = []
        if payload.booking_ids:
            result = await session.execute(
                select(LessonBooking).where(LessonBooking.id.in_(payload.booking_ids))
            )
            bookings = list(result.scalars().all())
            if len(bookings) != len(payload.booking_ids) or any(
                item.profile_id != profile.id or item.student_email != email for item in bookings
            ):
                raise HTTPException(status_code=400, detail="A selected lesson is invalid.")
            if any(
                item.status == "cancelled" and not item.cancellation_consumes_credit
                for item in bookings
            ):
                raise HTTPException(
                    status_code=400,
                    detail="An on-time cancelled lesson cannot consume a payment credit.",
                )
            existing = await session.execute(
                select(func.count(LessonPaymentAllocation.id)).where(
                    LessonPaymentAllocation.booking_id.in_(payload.booking_ids)
                )
            )
            if existing.scalar_one():
                raise HTTPException(status_code=409, detail="A selected lesson is already paid.")
        payment = StudentPayment(
            profile_id=profile.id,
            student_email=email,
            lessons_purchased=payload.lessons_purchased,
            amount_cents=payload.amount_cents,
            paid_at=datetime.now(UTC),
            valid_from=min((item.starts_at for item in bookings), default=None),
            expires_at=(
                min(item.starts_at for item in bookings) + timedelta(weeks=5)
                if bookings
                else None
            ),
        )
        session.add(payment)
        await session.flush()
        session.add_all(
            LessonPaymentAllocation(payment_id=payment.id, booking_id=item.id)
            for item in bookings
        )
        await session.commit()
        return {
            "id": str(payment.id),
            "lessons_purchased": payment.lessons_purchased,
            "allocated": len(bookings),
            "remaining": payment.lessons_purchased - len(bookings),
        }


@router.post("/api/scheduling/calendars/discover")
async def discover_calendars(request: Request) -> dict[str, object]:
    _require_same_origin(request)
    async with async_session_factory() as session:
        _, _, profile = await _management_profile(request, session)
        connections = await CalendarRepository(session).list_google_connections(
            household_id=profile.household_id
        )
        connection_refs = [(item.id, item.account_email) for item in connections]
        count = 0
        failures: list[dict[str, str]] = []
        for connection_id, account_email in connection_refs:
            try:
                count += await SchedulingService(session).discover_google_calendars(
                    profile=profile,
                    connection_id=connection_id,
                )
                await CalendarService(session).sync_google_connections(
                    household_id=profile.household_id,
                    connection_id=connection_id,
                )
            except (CalendarSyncError, httpx.HTTPError) as exc:
                await session.rollback()
                status_code = (
                    exc.response.status_code
                    if isinstance(exc, httpx.HTTPStatusError)
                    else getattr(exc, "status_code", None)
                )
                failures.append(
                    {
                        "connection_id": str(connection_id),
                        "account_email": account_email or "Google account",
                        "message": (
                            "Reconnect this account and grant Google Calendar access."
                            if status_code in {401, 403}
                            else "Google Calendar could not be reached. Try again."
                        ),
                    }
                )
        return {"discovered": count, "failures": failures}


@router.put("/api/scheduling/calendars/{calendar_id}")
async def update_calendar_selection(
    request: Request, calendar_id: UUID, payload: CalendarSelectionRequest
) -> dict[str, object]:
    _require_same_origin(request)
    async with async_session_factory() as session:
        _, _, profile = await _management_profile(request, session)
        item = await session.get(SchedulingCalendar, calendar_id)
        if item is None or item.profile_id != profile.id:
            raise HTTPException(status_code=404, detail="Calendar not found.")
        item.include_in_conflicts = payload.include_in_conflicts
        await session.commit()
        if payload.include_in_conflicts:
            connection = await session.get(CalendarConnection, item.connection_id)
            try:
                if connection is not None and connection.provider == CalendarProvider.icloud:
                    await CalendarService(session).sync_icloud_connections(
                        household_id=profile.household_id
                    )
                else:
                    await CalendarService(session).sync_google_connections(
                        household_id=profile.household_id
                    )
            except (CalendarSyncError, httpx.HTTPError):
                return {
                    "saved": True,
                    "sync_warning": "Calendar selected; its first sync will retry within five minutes.",
                }
        return {"saved": True}


@router.post("/api/scheduling/icloud-connections")
async def connect_icloud(
    request: Request, payload: ICloudConnectionRequest
) -> dict[str, object]:
    _require_same_origin(request)
    account_email = payload.account_email.strip().casefold()
    if not _email_is_valid(account_email):
        raise HTTPException(status_code=400, detail="Enter a valid Apple Account email.")
    app_password = payload.app_specific_password.get_secret_value().strip()
    if len(app_password) < 8:
        raise HTTPException(status_code=400, detail="Enter an Apple app-specific password.")
    async with async_session_factory() as session:
        user, household, profile = await _management_profile(request, session)
        repository = CalendarRepository(session)
        connection = await repository.upsert_icloud_connection(
            user_id=user.id,
            household_id=household.id,
            account_email=account_email,
            encrypted_app_password=CredentialCipher().encrypt(app_password),
            commit=False,
        )
        service = CalendarService(session)
        try:
            discovered = await service.discover_icloud_calendars(
                profile=profile, connection=connection
            )
            if discovered == 0:
                raise CalDAVProtocolError("No iCloud calendars were found.")
        except (CalendarSyncError, httpx.HTTPError, ValueError) as exc:
            await session.rollback()
            detail = (
                "Apple rejected the account email or app-specific password."
                if isinstance(exc, ICloudAuthenticationError)
                else "The iCloud calendars could not be discovered. Try reconnecting shortly."
            )
            raise HTTPException(status_code=400, detail=detail) from exc
        sync_warning = None
        try:
            await service.sync_icloud_connections(
                household_id=household.id, connection_id=connection.id
            )
        except (CalendarSyncError, httpx.HTTPError, ValueError):
            sync_warning = "Connected; the first event sync will retry within five minutes."
        return {
            "id": str(connection.id),
            "account_email": account_email,
            "calendar_count": discovered,
            "sync_warning": sync_warning,
        }


@router.post("/api/scheduling/icloud-connections/{connection_id}/refresh")
async def refresh_icloud(request: Request, connection_id: UUID) -> dict[str, int]:
    _require_same_origin(request)
    async with async_session_factory() as session:
        _, household, profile = await _management_profile(request, session)
        connection = await session.get(CalendarConnection, connection_id)
        if (
            connection is None
            or connection.household_id != household.id
            or connection.provider != CalendarProvider.icloud
        ):
            raise HTTPException(status_code=404, detail="iCloud connection not found.")
        try:
            discovered = await CalendarService(session).discover_icloud_calendars(
                profile=profile, connection=connection
            )
            synced = await CalendarService(session).sync_icloud_connections(
                household_id=household.id, connection_id=connection.id
            )
        except (CalendarSyncError, httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="iCloud refresh failed. Reconnect with a new app-specific password.",
            ) from exc
        return {"discovered": discovered, "synced": synced}


@router.delete("/api/scheduling/icloud-connections/{connection_id}")
async def disconnect_icloud(request: Request, connection_id: UUID) -> dict[str, bool]:
    _require_same_origin(request)
    async with async_session_factory() as session:
        _, household, _ = await _management_profile(request, session)
        deleted = await CalendarRepository(session).delete_icloud_connection(
            connection_id=connection_id, household_id=household.id
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="iCloud connection not found.")
        return {"deleted": True}


@router.post("/api/scheduling/ical-feeds")
async def add_ical_feed(request: Request, payload: ICalRequest) -> dict[str, object]:
    _require_same_origin(request)
    try:
        calendar_url = await validate_public_https_url(payload.url)
    except UnsafeExternalURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    async with async_session_factory() as session:
        user, household, _ = await _management_profile(request, session)
        feed = ICalFeed(
            user_id=user.id,
            household_id=household.id,
            name=payload.name.strip(),
            url=calendar_url,
            is_active=True,
        )
        session.add(feed)
        await session.commit()
        await session.refresh(feed)
        try:
            await CalendarService(session).sync_ical_feeds(feed_id=feed.id)
        except (UnsafeExternalURLError, httpx.HTTPError, ValueError) as exc:
            await session.execute(
                delete(CalendarEventCache).where(CalendarEventCache.source_id == feed.id)
            )
            await session.delete(feed)
            await session.commit()
            raise HTTPException(
                status_code=400,
                detail="The calendar feed could not be read and was not added.",
            ) from exc
        return {"id": str(feed.id), "name": feed.name, "is_active": True}


@router.delete("/api/scheduling/ical-feeds/{feed_id}")
async def delete_ical_feed(request: Request, feed_id: UUID) -> dict[str, bool]:
    _require_same_origin(request)
    async with async_session_factory() as session:
        _, household, _ = await _management_profile(request, session)
        await session.execute(
            delete(CalendarEventCache).where(CalendarEventCache.source_id == feed_id)
        )
        result = await session.execute(
            delete(ICalFeed).where(ICalFeed.id == feed_id, ICalFeed.household_id == household.id)
        )
        if not result.rowcount:
            raise HTTPException(status_code=404, detail="Calendar feed not found.")
        await session.commit()
        return {"deleted": True}


@router.get("/book/{slug}", response_class=HTMLResponse)
async def public_booking_page(request: Request, slug: str) -> HTMLResponse:
    safe_slug = html.escape(slug, quote=True)
    student_email = str(request.session.get("student_google_email") or "").strip().casefold()
    signed_in = bool(student_email)
    page = PUBLIC_HTML.replace("__BOOKING_SLUG__", safe_slug)
    if signed_in:
        page = page.replace(
            f'<a id="my-lessons" href="/book/{safe_slug}/lessons">My lessons</a>',
            '<div class="page-actions" id="account">'
            f'<a id="my-lessons" href="/book/{safe_slug}/lessons">My lessons</a>'
            '<div class="signed-in"><span>Signed in as '
            f'<strong>{html.escape(student_email)}</strong></span>'
            f'<form method="post" action="/auth/student/logout?slug={safe_slug}">'
            '<button type="submit">Sign out</button></form></div></div>',
        )
    page = page.replace(
        '<form id="booking"',
        SELECTED_SUMMARY_HTML + PUBLIC_INFO_HTML + '<form id="booking"',
    )
    page = page.replace(
        "Google sign-in keeps your lessons and permanent Meet link together.",
        "Google sign-in keeps your lessons, permanent Meet link, remaining sessions, "
        "cancellations, and rescheduling together.",
    )
    page = page.replace("__STUDENT_SIGNED_IN__", "true" if signed_in else "false")
    return HTMLResponse(page)


@router.get("/book/{slug}/lessons", response_class=HTMLResponse)
async def student_lessons_page(request: Request, slug: str):
    if not request.session.get("student_google_email"):
        return RedirectResponse(f"/auth/google/start?next=book:{slug}", status_code=303)
    return HTMLResponse(STUDENT_HTML.replace("__BOOKING_SLUG__", html.escape(slug, quote=True)))


@router.get("/api/public/scheduling/{slug}/my-lessons")
async def student_lessons(request: Request, slug: str) -> dict[str, object]:
    email, name = _student_identity(request)
    async with async_session_factory() as session:
        repository = SchedulingRepository(session)
        profile = await repository.profile_by_slug(slug=slug)
        if profile is None:
            raise HTTPException(status_code=404, detail="Booking page not found.")
        bookings = await repository.student_bookings(
            profile_id=profile.id, student_email=email
        )
        payments = await repository.student_payments(
            profile_id=profile.id, student_email=email
        )
        paid_booking_ids = await repository.allocation_booking_ids(
            payment_ids=[item.id for item in payments]
        )
        payment_allocations = {
            item.id: await repository.allocation_booking_ids(payment_ids=[item.id])
            for item in payments
        }
        now = datetime.now(UTC)
        return {
            "student_name": name,
            "student_email": email,
            "tutor_name": profile.display_name,
            "purchased": sum(item.lessons_purchased for item in payments),
            "allocated": len(paid_booking_ids),
            "remaining": sum(item.lessons_purchased for item in payments)
            - len(paid_booking_ids),
            "packages": [
                {
                    "lessons_purchased": item.lessons_purchased,
                    "allocated": len(payment_allocations[item.id]),
                    "remaining": item.lessons_purchased
                    - len(payment_allocations[item.id]),
                    "valid_from": item.valid_from.isoformat() if item.valid_from else None,
                    "expires_at": item.expires_at.isoformat() if item.expires_at else None,
                }
                for item in payments
            ],
            "lessons": [
                {
                    "id": str(item.id),
                    "starts_at": item.starts_at.isoformat(),
                    "ends_at": item.ends_at.isoformat(),
                    "status": item.status,
                    "paid": item.id in paid_booking_ids,
                    "is_past": item.ends_at <= now,
                    "can_cancel": item.status == "confirmed" and item.starts_at > now,
                    "late": item.starts_at - now < timedelta(hours=12),
                    "meeting_url": item.meeting_url,
                }
                for item in bookings
            ],
        }


@router.post("/api/public/scheduling/{slug}/my-lessons/{booking_id}/cancel")
async def student_cancel_lesson(
    request: Request, slug: str, booking_id: UUID
) -> dict[str, object]:
    _require_same_origin(request)
    email, _ = _student_identity(request)
    async with async_session_factory() as session:
        repository = SchedulingRepository(session)
        profile = await repository.profile_by_slug(slug=slug)
        booking = await session.get(LessonBooking, booking_id)
        if (
            profile is None
            or booking is None
            or booking.profile_id != profile.id
            or booking.student_email != email
        ):
            raise HTTPException(status_code=404, detail="Lesson not found.")
        if booking.status == "cancelled":
            await _remove_booking_calendar_event(
                session=session,
                household_id=profile.household_id,
                booking=booking,
            )
            await session.commit()
            return {"cancelled": True, "credit_restored": False}
        now = datetime.now(UTC)
        if booking.starts_at <= now:
            raise HTTPException(status_code=409, detail="Past lessons cannot be cancelled.")
        await _remove_booking_calendar_event(
            session=session,
            household_id=profile.household_id,
            booking=booking,
        )
        late = booking.starts_at - now < timedelta(hours=12)
        allocation = await repository.allocation_for_booking(booking_id=booking.id)
        credit_restored = allocation is not None and not late
        if credit_restored:
            await session.delete(allocation)
        booking.status = "cancelled"
        booking.cancelled_at = now
        booking.cancellation_consumes_credit = late
        await session.commit()
        return {"cancelled": True, "credit_restored": credit_restored, "late": late}


@router.get("/api/public/scheduling/{slug}")
async def public_scheduling_profile(slug: str) -> dict[str, object]:
    async with async_session_factory() as session:
        repository = SchedulingRepository(session)
        profile = await repository.profile_by_slug(slug=slug)
        if profile is None:
            raise HTTPException(status_code=404, detail="Booking page not found.")
        return {
            "display_name": profile.display_name,
            "timezone": profile.timezone,
            "booking_window_days": profile.booking_window_days,
            "lesson_types": [
                _lesson_type_json(item)
                for item in await repository.list_lesson_types(
                    profile_id=profile.id, active_only=True
                )
            ],
        }


@router.get("/api/public/scheduling/{slug}/slots")
async def public_slots(
    slug: str,
    lesson_type_id: UUID,
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
) -> dict[str, object]:
    async with async_session_factory() as session:
        repository = SchedulingRepository(session)
        profile = await repository.profile_by_slug(slug=slug)
        if profile is None:
            raise HTTPException(status_code=404, detail="Booking page not found.")
        lesson_type = await repository.lesson_type(
            profile_id=profile.id, lesson_type_id=lesson_type_id
        )
        if lesson_type is None or not lesson_type.is_active:
            raise HTTPException(status_code=404, detail="Lesson type not found.")
        try:
            slots = await SchedulingService(session).slots(
                profile=profile,
                lesson_type=lesson_type,
                start_day=start,
                end_day=end,
            )
        except SchedulingValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"slots": [slot.isoformat() for slot in slots], "timezone": profile.timezone}


@router.post("/api/public/scheduling/{slug}/book", status_code=201)
async def public_book(request: Request, slug: str, payload: BookingRequest) -> dict[str, object]:
    student_email, student_name = _student_identity(request)
    if payload.website:
        raise HTTPException(status_code=400, detail="Invalid booking request.")
    async with async_session_factory() as session:
        repository = SchedulingRepository(session)
        profile = await repository.profile_by_slug(slug=slug)
        if profile is None:
            raise HTTPException(status_code=404, detail="Booking page not found.")
        lesson_type = await repository.lesson_type(
            profile_id=profile.id, lesson_type_id=payload.lesson_type_id
        )
        if lesson_type is None or not lesson_type.is_active:
            raise HTTPException(status_code=404, detail="Lesson type not found.")
        try:
            bookings = await SchedulingService(session).book_many(
                profile=profile,
                lesson_type=lesson_type,
                starts_at=payload.starts_at,
                student_name=student_name,
                student_email=student_email,
                student_timezone=payload.student_timezone,
                notes=payload.notes,
            )
        except (SchedulingValidationError, SlotUnavailableError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (
            CalendarEventMatchError,
            CalendarNotConnectedError,
            CalendarSyncError,
            CalendarWritePermissionError,
            UnsafeExternalURLError,
            httpx.HTTPError,
            ValueError,
        ) as exc:
            raise HTTPException(
                status_code=503,
                detail="Calendars could not be checked safely. Please try again shortly.",
            ) from exc
        return {
            "count": len(bookings),
            "bookings": [
                {
                    "id": str(booking.id),
                    "status": booking.status,
                    "starts_at": booking.starts_at.isoformat(),
                    "ends_at": booking.ends_at.isoformat(),
                }
                for booking in bookings
            ],
        }


MANAGEMENT_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lesson scheduling</title><style>
:root{font-family:"Google Sans",Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;color:#29213a;background:#f8f6fc;--purple:#7357c7;--purple-dark:#6042b3;--purple-soft:#eee9fb;--purple-pale:#f7f3ff;--line:#e6e0ef;--muted:#756d82;--danger:#a6404a;--danger-soft:#fff0f1}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 8% 0%,#eee8ff 0,transparent 30%),radial-gradient(circle at 96% 10%,#f6eafb 0,transparent 28%),#f8f6fc}
main{max-width:1440px;margin:auto;padding:36px 28px 88px}header{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-bottom:26px}
h1{font-size:clamp(30px,4vw,46px);letter-spacing:-.035em;margin:0}h2{font-size:22px;letter-spacing:-.02em;margin:0 0 18px}.muted{color:var(--muted);line-height:1.45}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;align-items:start}.column{display:grid;gap:20px;align-content:start;min-width:0}
.card{min-width:0;background:rgba(255,255,255,.94);border:1px solid rgba(221,212,235,.9);border-radius:22px;padding:24px;box-shadow:0 12px 34px rgba(67,47,98,.08);backdrop-filter:blur(12px)}
.tabs{display:inline-flex;gap:5px;margin:0 0 24px;padding:5px;border:1px solid var(--line);border-radius:999px;background:rgba(255,255,255,.78);box-shadow:0 8px 24px rgba(67,47,98,.06)}.tab{min-width:112px;background:transparent;color:var(--muted);box-shadow:none}.tab:hover{background:var(--purple-soft);color:var(--purple-dark);box-shadow:none;transform:none}.tab.active{background:linear-gradient(135deg,#6372d8,#8559cf);color:#fff;box-shadow:0 5px 14px rgba(96,66,179,.2)}.tab-panel[hidden]{display:none}
.overview{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-bottom:20px}.metric{padding:20px 22px;border:1px solid var(--line);border-radius:18px;background:linear-gradient(135deg,rgba(255,255,255,.97),rgba(247,244,255,.94));box-shadow:0 9px 25px rgba(67,47,98,.06)}.metric strong{display:block;font-size:30px;letter-spacing:-.03em}.metric span{color:var(--muted);font-size:13px}.lessons-grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(340px,.75fr);gap:20px;align-items:start}.student-tools{display:flex;gap:10px;align-items:center}.student-tools input{flex:1}.student-list{max-height:510px;overflow:auto;padding-right:4px}.student-card{width:100%;min-height:0;padding:13px 14px;border:1px solid var(--line);border-radius:15px;background:#fefeff;color:inherit;box-shadow:none;text-align:left;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center}.student-card:hover,.student-card.active{background:linear-gradient(135deg,#f2f5ff,#f7f0ff);border-color:#cfc6e8;color:inherit;box-shadow:none;transform:none}.student-card strong,.student-card span{display:block}.student-card small{display:block;margin-top:4px;color:var(--muted);font-weight:500;overflow:hidden;text-overflow:ellipsis}.balance{min-width:58px;text-align:center;padding:7px 9px;border-radius:12px;background:var(--purple-soft);color:var(--purple-dark);font-size:12px;font-weight:800}.balance b{display:block;font-size:18px}.empty{padding:18px;border:1px dashed #d8d0e5;border-radius:15px;color:var(--muted);text-align:center}.payment-card{position:sticky;top:18px}.payment-card .list{max-height:260px;overflow:auto}
form{display:grid;gap:14px}label{display:grid;gap:7px;font-size:13px;font-weight:650;margin:0}input,select,textarea,button{font:inherit}input,select,textarea{width:100%;min-height:44px;padding:10px 13px;border:1px solid #d9d1e4;border-radius:12px;background:#fff;color:inherit;outline:none;transition:border-color .16s,box-shadow .16s,background .16s}textarea{min-height:88px;resize:vertical}input:hover,select:hover,textarea:hover{border-color:#c4b6da}input:focus,select:focus,textarea:focus{border-color:var(--purple);box-shadow:0 0 0 3px rgba(115,87,199,.14);background:#fefeff}
button,.button{min-height:42px;border:1px solid transparent;border-radius:999px;padding:10px 17px;background:var(--purple);color:#fff;font-weight:700;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;box-shadow:0 5px 14px rgba(96,66,179,.16);transition:transform .14s,box-shadow .14s,background .14s}button:hover,.button:hover{background:var(--purple-dark);box-shadow:0 7px 18px rgba(96,66,179,.23);transform:translateY(-1px)}button:focus-visible,.button:focus-visible{outline:3px solid rgba(115,87,199,.24);outline-offset:2px}.secondary{background:var(--purple-soft);border-color:#e2d9f7;color:#584396;box-shadow:none}.secondary:hover{background:#e4dcf8;color:#4d378d}.danger{background:var(--danger-soft);border-color:#f8dfe2;color:var(--danger);box-shadow:none}.danger:hover{background:#fbe3e6;color:#92353f}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.row>button,.row>.button,form>button{margin-top:4px;justify-self:start}#profile>.row:not(label){display:grid;grid-template-columns:1fr}label.row{display:flex;align-items:center;min-height:34px}label.row input{min-height:auto;accent-color:var(--purple)}.list{display:grid;gap:10px;margin-top:16px}.item{display:flex;gap:14px;align-items:center;justify-content:space-between;border:1px solid var(--line);border-radius:15px;padding:12px 14px;background:#fefeff}.item p{margin:3px 0}.item button{min-height:36px;padding:7px 13px}.item-actions{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end}.help{font-size:12px;margin:0}.help a{color:var(--purple-dark)}
.availability-card .list{gap:10px}.days{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) 42px;gap:8px;padding:10px;border:1px solid var(--line);border-radius:16px;background:var(--purple-pale);transition:opacity .16s,background .16s}.days select{grid-column:1/3}.days input{min-width:0}.days.disabled{background:#faf9fc;opacity:.68}.days.disabled select,.days.disabled input{color:#8c8595;background:#f4f1f7}.days .day-toggle{grid-column:3;grid-row:1/3;align-self:stretch;min-height:0;width:42px;padding:0;border:1px solid #e5deed;border-radius:13px;background:#fff;color:#796b8c;box-shadow:none}.days .day-toggle:hover{background:#f8edf1;border-color:#efd9df;color:#9f4450;box-shadow:none;transform:none}.days.disabled .day-toggle{background:var(--purple-soft);border-color:#ddd3f2;color:var(--purple)}.days.disabled .day-toggle:hover{background:#e5dcf8;color:var(--purple-dark)}.days .day-toggle svg{width:18px;height:18px;pointer-events:none}.availability-card>.row{margin-top:16px}.status{position:fixed;left:50%;bottom:22px;z-index:10;min-height:0;transform:translateX(-50%);padding:10px 16px;border-radius:999px;background:#302443;color:#fff;box-shadow:0 10px 30px rgba(48,36,67,.2)}.status:empty{display:none}.hidden{position:absolute;left:-9999px}
@media(max-width:980px){main{padding:28px 20px 72px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.lessons-grid{grid-template-columns:1fr}.payment-card{position:static}}
@media(max-width:680px){main{padding:22px 14px 60px}header{align-items:flex-start}.grid{grid-template-columns:1fr;gap:14px}.card{padding:20px;border-radius:18px}.days{grid-template-columns:minmax(0,1fr) minmax(0,1fr) auto}.overview{grid-template-columns:1fr}.tabs{display:flex}.tab{flex:1;min-width:0}.student-tools{align-items:stretch;flex-direction:column}}
</style></head><body><main><header><h1>Lesson scheduling</h1></header><nav class="tabs" aria-label="Dashboard sections"><button class="tab" type="button" data-tab="setup">Setup</button><button class="tab active" type="button" data-tab="lessons">Lessons</button></nav>
<section class="tab-panel" id="setup-panel" hidden><div class="grid"><div class="column"><section class="card"><h2>Booking page</h2><form id="profile"><label>Your public name<input name="display_name" required></label><label>Booking link<input name="slug" required></label><label>Timezone<input name="timezone" required></label><div class="row"><label>Minimum notice (minutes)<input name="minimum_notice_minutes" type="number" min="0"></label><label>Booking window (days)<input name="booking_window_days" type="number" min="1"></label></div><div class="row"><label>Commute time before a lesson (minutes)<input name="buffer_before_minutes" type="number" min="0"></label><label>Commute time after a lesson (minutes)<input name="buffer_after_minutes" type="number" min="0"></label><label>Start-time increments (minutes)<input name="slot_interval_minutes" type="number" min="5"><span class="muted">For example, 15 offers 09:00, 09:15, 09:30, and so on.</span></label></div><p class="muted">Commute time is required only around non-lesson calendar events. Lessons can be booked back-to-back.</p><label>Calendar for new lessons<select name="booking_calendar_id"><option value="">Primary Google calendar</option></select></label><label class="row"><input name="is_active" type="checkbox" style="width:auto">Accept bookings</label><button>Save settings</button></form><p id="public-link"></p></section>
<section class="card"><h2>Google calendars</h2><p class="muted">Selected calendars block lesson availability. Reconnect if calendar discovery asks for permission.</p><div class="list" id="google-accounts"></div><div class="row"><a class="button secondary" href="/calendar/google/start?next=scheduling">Connect another account</a><button id="discover" type="button">Refresh calendars</button></div><div class="list" id="calendars"></div></section></div>
<div class="column"><section class="card availability-card"><h2>Weekly availability</h2><p class="muted">Set times for all seven days. Disable any day you do not teach. Times use your timezone.</p><div class="list" id="availability"></div><div class="row"><button id="add-window" class="secondary" type="button">Add another time</button><button id="save-availability" type="button">Save availability</button></div></section>
<section class="card"><h2>iCloud calendars</h2><p class="muted">Connect privately with an Apple app-specific password. Access is read-only and your calendars remain private.</p><form id="icloud"><label>Apple Account email<input name="account_email" type="email" autocomplete="username" required></label><label>App-specific password<input name="app_specific_password" type="password" autocomplete="new-password" required></label><p class="muted help">Use a password created for this scheduler, never your main Apple password. <a href="https://account.apple.com/account/manage/section/security" target="_blank" rel="noopener">Create one in Apple Account security</a>.</p><button>Connect iCloud</button></form><div class="list" id="icloud-accounts"></div><div class="list" id="icloud-calendars"></div></section></div>
<div class="column"><section class="card"><h2>Lesson types</h2><form id="lesson"><input name="id" type="hidden"><label>Name<input name="name" required placeholder="English lesson"></label><label>Length (minutes)<input name="duration_minutes" type="number" min="15" value="60" required></label><label>Location or call link<input name="location"></label><label>Description<textarea name="description"></textarea></label><label class="row"><input name="is_active" type="checkbox" style="width:auto" checked>Active</label><button>Save lesson type</button></form><div class="list" id="lessons"></div></section></div></div></section>
<section class="tab-panel" id="lessons-panel"><div class="overview"><div class="metric"><strong id="upcoming-count">0</strong><span>Upcoming lessons</span></div><div class="metric"><strong id="student-count">0</strong><span>Students</span></div><div class="metric"><strong id="credit-count">0</strong><span>Paid lessons remaining</span></div></div><div class="lessons-grid"><section class="card"><h2>Upcoming lessons</h2><p class="muted">Your next confirmed lessons, in chronological order.</p><div class="list" id="bookings"></div></section><div class="column"><section class="card"><h2>Students and balances</h2><div class="student-tools"><input id="student-search" type="search" placeholder="Search name or email" aria-label="Search students"></div><div class="list student-list" id="students"></div></section><section class="card payment-card"><h2>Register payment</h2><p class="muted">Choose a student, record their package, and optionally assign it to unpaid lessons.</p><form id="payment"><label>Student email<input name="student_email" type="email" list="student-emails" required></label><datalist id="student-emails"></datalist><div class="row"><label>Lessons purchased<input name="lessons_purchased" type="number" min="1" max="100" required></label><label>Amount paid (€)<input name="amount_euros" type="number" min="0" step="0.01"></label></div><div class="list" id="payment-lessons"></div><button>Record payment</button></form></section></div></div></section><p class="status" id="status"></p></main>
<script>
const $=s=>document.querySelector(s), esc=s=>String(s??""); let state,googleDiscoveryFailures=new Map(),selectedStudentEmail='';
async function api(url,opt={}){const r=await fetch(url,{...opt,headers:{"Content-Type":"application/json",...(opt.headers||{})}});if(!r.ok){let m="Request failed";try{m=(await r.json()).detail||m}catch{}throw Error(m)}return r.status===204?null:r.json()}
function field(form,n,v){const e=form.elements[n];if(e.type==="checkbox")e.checked=!!v;else e.value=v??""}
async function load(){state=await api('/api/scheduling/manage');const f=$('#profile');for(const [k,v] of Object.entries(state.profile))if(f.elements[k])field(f,k,v);$('#public-link').innerHTML=`Public page: <a href="${state.profile.public_url}" target="_blank" rel="noopener">${state.profile.public_url}</a>`;render();renderBookingActions();renderStudents();renderMetrics()}
function render(){
 const names=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
 $('#lessons').replaceChildren(...state.lesson_types.map(x=>node(`${x.name} · ${x.duration_minutes} min`,x.is_active?'Active':'Inactive',()=>editLesson(x))));
 const googleCalendars=state.calendars.filter(x=>x.provider==='google'),icloudCalendars=state.calendars.filter(x=>x.provider==='icloud');
 const calSelect=$('#profile').elements.booking_calendar_id;
 calSelect.replaceChildren(new Option('Primary Google calendar',''),...googleCalendars.filter(x=>x.can_write).map(x=>new Option(`${x.name}${x.account_email?' · '+x.account_email:''}`,x.external_calendar_id)));
 calSelect.value=state.profile.booking_calendar_id||'';
 $('#google-accounts').replaceChildren(...(state.google_accounts.length?state.google_accounts.map(x=>node(x.account_email||'Google account',googleDiscoveryFailures.get(x.id)||`${x.calendar_count} calendar${x.calendar_count===1?'':'s'} found`)):[node('No Google account connected','Connect an account to check its calendars.')]));
 $('#calendars').replaceChildren(...(googleCalendars.length?googleCalendars.map(calendarToggleNode):[node('No calendars discovered','Use Refresh calendars, or reconnect the account if Google asks for permission.')]));
 $('#icloud-accounts').replaceChildren(...state.icloud_accounts.map(icloudAccountNode));
 $('#icloud-calendars').replaceChildren(...(icloudCalendars.length?icloudCalendars.map(calendarToggleNode):[node('No iCloud calendars connected','Enter your Apple Account email and an app-specific password above.')]));
 const windows=[];for(let day=0;day<7;day++){const rules=state.availability.filter(x=>x.weekday===day);if(rules.length)windows.push(...rules.map(x=>windowNode(day,x.starts_at,x.ends_at,names,true)));else windows.push(windowNode(day,'09:00','17:00',names,false))}$('#availability').replaceChildren(...windows)
}
function node(a,b,click,label='Edit'){const d=document.createElement('div');d.className='item';const s=document.createElement('span'),p=document.createElement('strong'),m=document.createElement('p');p.textContent=a;m.textContent=b;m.className='muted';s.append(p,m);d.append(s);if(click){const bt=document.createElement('button');bt.className=label==='Edit'?'secondary':'danger';bt.textContent=label;bt.onclick=click;d.append(bt)}return d}
function calendarToggleNode(x){const d=document.createElement('label');d.className='item';const t=document.createElement('span');t.textContent=`${x.name}${x.account_email?' · '+x.account_email:''} (${x.access_role||'read'})`;const c=document.createElement('input');c.type='checkbox';c.style.width='auto';c.checked=x.include_in_conflicts;c.onchange=async()=>{const result=await api(`/api/scheduling/calendars/${x.id}`,{method:'PUT',body:JSON.stringify({include_in_conflicts:c.checked})});status(result.sync_warning||'Calendar selection saved.')};d.append(t,c);return d}
function icloudAccountNode(x){const d=node(x.account_email||'Apple Account',`${x.calendar_count} calendar${x.calendar_count===1?'':'s'} found`),actions=document.createElement('div'),refresh=document.createElement('button'),disconnect=document.createElement('button');actions.className='item-actions';refresh.type='button';refresh.className='secondary';refresh.textContent='Refresh';refresh.onclick=async()=>{const result=await api(`/api/scheduling/icloud-connections/${x.id}/refresh`,{method:'POST',body:'{}'});await load();status(`Refreshed ${result.discovered} calendar${result.discovered===1?'':'s'}.`)};disconnect.type='button';disconnect.className='danger';disconnect.textContent='Disconnect';disconnect.onclick=async()=>{if(!confirm(`Disconnect ${x.account_email}?`))return;await api(`/api/scheduling/icloud-connections/${x.id}`,{method:'DELETE'});await load();status('iCloud disconnected.')};actions.append(refresh,disconnect);d.append(actions);return d}
function renderBookingActions(){const items=state.bookings.map(x=>node(x.student_name,new Date(x.starts_at).toLocaleString()+` · ${x.student_email}`,async()=>{if(!confirm(`Cancel the lesson with ${x.student_name}?`))return;await api(`/api/scheduling/bookings/${x.id}/cancel`,{method:'POST',body:'{}'});await load();status('Lesson cancelled.')},'Cancel'));$('#bookings').replaceChildren(...(items.length?items:[emptyNode('No upcoming lessons.')]))}
function emptyNode(text){const d=document.createElement('div');d.className='empty';d.textContent=text;return d}
function studentNode(x){const button=document.createElement('button'),copy=document.createElement('span'),name=document.createElement('strong'),email=document.createElement('small'),balance=document.createElement('span'),remaining=Math.max(0,x.purchased-x.allocated);button.type='button';button.className='student-card'+(x.email===selectedStudentEmail?' active':'');name.textContent=x.name;email.textContent=`${x.email} · ${x.allocated} assigned`;balance.className='balance';balance.innerHTML=`<b>${remaining}</b> left`;copy.append(name,email);button.append(copy,balance);button.onclick=()=>{selectedStudentEmail=x.email;$('#payment').elements.student_email.value=x.email;renderStudents();$('#payment').scrollIntoView({behavior:'smooth',block:'nearest'})};return button}
function renderStudents(){const input=$('#payment').elements.student_email,query=$('#student-search').value.trim().toLowerCase();if(!selectedStudentEmail&&input.value)selectedStudentEmail=input.value;if(selectedStudentEmail&&!input.value)input.value=selectedStudentEmail;$('#student-emails').replaceChildren(...state.students.map(x=>new Option(x.email)));const visible=state.students.filter(x=>!query||x.name.toLowerCase().includes(query)||x.email.toLowerCase().includes(query));$('#students').replaceChildren(...(visible.length?visible.map(studentNode):[emptyNode(query?'No matching students.':'Students appear after their first booking or payment.') ]));const student=state.students.find(x=>x.email===input.value);const bookings=student?student.bookings.filter(x=>!x.paid&&x.status!=='cancelled'):[];$('#payment-lessons').replaceChildren(...(bookings.length?bookings.map(x=>{const label=document.createElement('label'),box=document.createElement('input');label.className='row';box.type='checkbox';box.name='booking_id';box.value=x.id;box.style.width='auto';label.append(box,document.createTextNode(`${new Date(x.starts_at).toLocaleString()} · ${x.status}`));return label}):[emptyNode(student?'No unpaid lessons to assign.':'Select a student to assign lessons.')]))}
function renderMetrics(){$('#upcoming-count').textContent=state.bookings.length;$('#student-count').textContent=state.students.length;$('#credit-count').textContent=state.students.reduce((total,x)=>total+Math.max(0,x.purchased-x.allocated),0)}
function selectTab(name){document.querySelectorAll('.tab').forEach(button=>button.classList.toggle('active',button.dataset.tab===name));$('#setup-panel').hidden=name!=='setup';$('#lessons-panel').hidden=name!=='lessons';history.replaceState({},'',`${location.pathname}${location.search}#${name}`)}
const disableIcon='<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h16M9 7V4h6v3m-8 0 1 13h8l1-13M10 11v5m4-5v5"/></svg>',enableIcon='<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v8m-4-4h8"/></svg>';
function setWindowEnabled(d,enabled){d.classList.toggle('disabled',!enabled);d.querySelectorAll('select,input').forEach(control=>control.disabled=!enabled);const toggle=d.querySelector('.day-toggle'),day=d.querySelector('select').selectedOptions[0]?.text||'day';toggle.innerHTML=enabled?disableIcon:enableIcon;toggle.setAttribute('aria-label',enabled?`Disable ${day}`:`Enable ${day}`);toggle.title=enabled?`Disable ${day}`:`Enable ${day}`}
function windowNode(day=0,start='09:00',end='17:00',names=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'],enabled=true){const d=document.createElement('div');d.className='days';const s=document.createElement('select');s.setAttribute('aria-label','Day');names.forEach((n,i)=>s.add(new Option(n,i)));s.value=day;const a=document.createElement('input');a.type='time';a.setAttribute('aria-label','Available from');a.value=start;const b=document.createElement('input');b.type='time';b.setAttribute('aria-label','Available until');b.value=end;const toggle=document.createElement('button');toggle.type='button';toggle.className='day-toggle';toggle.onclick=()=>setWindowEnabled(d,d.classList.contains('disabled'));s.onchange=()=>setWindowEnabled(d,!d.classList.contains('disabled'));d.append(s,a,b,toggle);setWindowEnabled(d,enabled);return d}
function editLesson(x){const f=$('#lesson');for(const k of ['id','name','duration_minutes','location','description','is_active'])field(f,k,x[k]);f.scrollIntoView({behavior:'smooth'})}function status(s){$('#status').textContent=s;setTimeout(()=>$('#status').textContent='',4000)}
$('#profile').onsubmit=async e=>{e.preventDefault();const f=e.currentTarget,d=Object.fromEntries(new FormData(f));for(const n of ['minimum_notice_minutes','booking_window_days','buffer_before_minutes','buffer_after_minutes','slot_interval_minutes'])d[n]=Number(d[n]);d.is_active=f.elements.is_active.checked;await api('/api/scheduling/profile',{method:'PUT',body:JSON.stringify(d)});status('Settings saved.');await load()};
$('#lesson').onsubmit=async e=>{e.preventDefault();const f=e.currentTarget,d=Object.fromEntries(new FormData(f)),id=d.id;d.duration_minutes=Number(d.duration_minutes);d.is_active=f.elements.is_active.checked;delete d.id;await api(id?`/api/scheduling/lesson-types/${id}`:'/api/scheduling/lesson-types',{method:id?'PUT':'POST',body:JSON.stringify(d)});f.reset();f.elements.duration_minutes.value=60;f.elements.is_active.checked=true;status('Lesson type saved.');await load()};
$('#add-window').onclick=()=>$('#availability').append(windowNode());$('#save-availability').onclick=async()=>{const rules=[...$('#availability').children].filter(d=>!d.classList.contains('disabled')).map(d=>({weekday:Number(d.children[0].value),starts_at:d.children[1].value,ends_at:d.children[2].value}));await api('/api/scheduling/availability',{method:'PUT',body:JSON.stringify({rules})});status('Availability saved.');await load()};
$('#discover').onclick=async()=>{status('Checking Google calendars…');const result=await api('/api/scheduling/calendars/discover',{method:'POST',body:'{}'});googleDiscoveryFailures=new Map((result.failures||[]).map(x=>[x.connection_id,x.message]));await load();status(result.failures?.length?`Loaded ${result.discovered} calendar${result.discovered===1?'':'s'}; ${result.failures.length} account${result.failures.length===1?' needs':'s need'} attention.`:`Found ${result.discovered} calendar${result.discovered===1?'':'s'}.`)};
$('#icloud').onsubmit=async e=>{e.preventDefault();const form=e.currentTarget,d=Object.fromEntries(new FormData(form));status('Connecting privately to iCloud…');const result=await api('/api/scheduling/icloud-connections',{method:'POST',body:JSON.stringify(d)});form.reset();await load();status(result.sync_warning||`Connected ${result.calendar_count} iCloud calendar${result.calendar_count===1?'':'s'}.`)};
$('#payment').elements.student_email.onchange=e=>{selectedStudentEmail=e.currentTarget.value;renderStudents()};
$('#payment').elements.student_email.oninput=renderStudents;
$('#student-search').oninput=renderStudents;
document.querySelectorAll('.tab').forEach(button=>button.onclick=()=>selectTab(button.dataset.tab));selectTab(location.hash==='#setup'||new URLSearchParams(location.search).has('calendar')?'setup':'lessons');
$('#payment').onsubmit=async e=>{e.preventDefault();const form=e.currentTarget,data=new FormData(form),euros=data.get('amount_euros'),payload={student_email:data.get('student_email'),lessons_purchased:Number(data.get('lessons_purchased')),amount_cents:euros===''?null:Math.round(Number(euros)*100),booking_ids:data.getAll('booking_id')};await api('/api/scheduling/student-payments',{method:'POST',body:JSON.stringify(payload)});form.reset();await load();status('Payment recorded and selected lessons marked as paid.')};
document.addEventListener('unhandledrejection',e=>{e.preventDefault();status(e.reason?.message||'Something went wrong.')});load().then(()=>{const result=new URLSearchParams(location.search).get('calendar');if(result==='connected')status('Google Calendar connected and calendars loaded.');else if(result)status('Google connected, but calendars could not be loaded. Please reconnect and grant calendar access.');if(result)history.replaceState({},'',location.pathname)}).catch(e=>status(e.message));
</script></body></html>"""


SELECTED_SUMMARY_HTML = r"""<section id="selected-summary" class="selected-summary hidden" aria-live="polite"><div><h3>Selected lessons</h3><p class="muted">Review your dates before confirming.</p></div><div id="selected-list" class="selected-list"></div></section>"""


PUBLIC_INFO_HTML = r"""<section class="booking-info"><div><h3>Pricing</h3><p class="muted">Single lesson — €30<br>8 lessons — €224 (€28/hour)<br>12 lessons — €324 (€27/hour)<br>20 lessons — €500 (€25/hour)</p></div><div><h3>Booking conditions</h3><p class="muted">Packages are valid for five weeks from the first booked lesson. Book up to 10 lessons at once. Cancellations and rescheduling at least 12 hours ahead restore the lesson credit. Later changes remain available but consume the credit.</p></div></section>"""


STUDENT_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>My lessons</title><style>
:root{font-family:"Google Sans",Inter,system-ui,sans-serif;color:#28243f;background:#f8f7ff;--indigo:#5f72dc;--violet:#8b5bd6;--line:#dedff1;--muted:#716d87}*{box-sizing:border-box}body{margin:0;min-height:100vh;padding:42px 20px;background:radial-gradient(circle at 10% 5%,#e9efff 0,transparent 34%),radial-gradient(circle at 92% 8%,#f3eaff 0,transparent 31%),#f8f7ff}main{width:min(900px,100%);margin:auto}.header,.summary,.lesson{background:#fff;border:1px solid var(--line);border-radius:20px;box-shadow:0 14px 35px rgba(75,73,145,.08)}.header{padding:26px 30px;display:flex;justify-content:space-between;align-items:center;gap:18px}.header-actions{display:flex;align-items:center;justify-content:flex-end;gap:12px;flex-wrap:wrap}.account{display:flex;align-items:center;gap:10px;padding:7px 8px 7px 12px;border:1px solid var(--line);border-radius:14px;background:linear-gradient(135deg,#f7f9ff,#faf6ff)}.account-label{color:var(--muted);font-size:11px;line-height:1.25}.account-label strong{display:block;max-width:230px;color:#494566;font-size:12px;overflow:hidden;text-overflow:ellipsis}.account form{display:flex}.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;margin:18px 0;overflow:hidden}.metric{padding:22px;background:#fff}.metric strong{display:block;font-size:25px}.metric span,.muted{color:var(--muted);font-size:13px}.list{display:grid;gap:12px}.lesson{padding:18px 20px;display:flex;justify-content:space-between;align-items:center;gap:18px}.lesson.past{opacity:.5;background:#f8f8fc;box-shadow:none}.actions{display:flex;gap:8px;flex-wrap:wrap}a,button{font:inherit;font-weight:750;border-radius:11px;padding:9px 13px;text-decoration:none;cursor:pointer}a.primary,button.primary{border:0;color:#fff;background:linear-gradient(135deg,var(--indigo),var(--violet))}button.secondary,a.secondary{border:1px solid var(--line);color:#514d76;background:#fff}.account button{padding:7px 10px;font-size:12px}.badge{display:inline-block;margin-top:6px;padding:4px 8px;border-radius:999px;background:#efefff;color:#5549b8;font-size:12px;font-weight:700}.notice{color:#5549b8;margin:14px 0 0;font-size:13px}.error{color:#a6405d}dialog{width:min(440px,calc(100% - 32px));padding:0;border:1px solid var(--line);border-radius:22px;color:#28243f;background:#fff;box-shadow:0 24px 70px rgba(53,48,103,.28)}dialog::backdrop{background:rgba(40,36,63,.36);backdrop-filter:blur(3px)}.modal-body{padding:28px}.modal-icon{width:42px;height:42px;display:grid;place-items:center;margin-bottom:18px;border-radius:14px;background:linear-gradient(135deg,#e5ecff,#eee2ff);color:#6257bd;font-size:21px}.modal-body h2{margin:0 0 9px;font-size:22px}.modal-body p{margin:0;color:var(--muted);font-size:14px;line-height:1.55}.modal-note{margin-top:12px!important;padding:11px 12px;border-radius:11px;background:#f7f5ff;color:#5e5877!important;font-size:12px!important}.modal-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:24px}.modal-actions button{min-width:100px}@media(max-width:720px){.header,.lesson{align-items:flex-start;flex-direction:column}.header-actions,.account{width:100%}.header-actions{justify-content:flex-start}.account{justify-content:space-between}.summary{grid-template-columns:1fr}.actions{width:100%}.modal-actions{flex-direction-reverse}.modal-actions button{width:100%}}
</style></head><body><main><section class="header"><div><h1>My lessons</h1><p class="muted" id="identity"></p></div><div class="header-actions"><a class="primary" href="/book/__BOOKING_SLUG__">Book lessons</a><div class="account"><span class="account-label">Signed in as<strong id="account-email"></strong></span><form method="post" action="/auth/student/logout?slug=__BOOKING_SLUG__"><button class="secondary" type="submit">Sign out</button></form></div></div></section><section class="summary"><div class="metric"><strong id="purchased">0</strong><span>Paid lessons</span></div><div class="metric"><strong id="allocated">0</strong><span>Assigned to lessons</span></div><div class="metric"><strong id="remaining">0</strong><span>Paid lessons remaining</span></div></section><div class="list" id="lessons"></div><p class="notice" id="notice" role="status"></p><p class="error" id="error"></p></main><dialog id="lesson-modal" aria-labelledby="modal-title"><div class="modal-body"><div class="modal-icon" aria-hidden="true">↻</div><h2 id="modal-title"></h2><p id="modal-message"></p><p class="modal-note">Changes made less than 12 hours before a lesson still use the lesson credit.</p><p class="error" id="modal-error" role="alert"></p><div class="modal-actions"><button class="secondary" id="modal-back" type="button">Keep lesson</button><button class="primary" id="modal-confirm" type="button"></button></div></div></dialog><script>
const slug='__BOOKING_SLUG__',$=s=>document.querySelector(s);async function api(u,o={}){const r=await fetch(u,{...o,headers:{'Content-Type':'application/json'}});if(!r.ok){let m='Request failed';try{m=(await r.json()).detail||m}catch{}throw Error(m)}return r.json()}function lessonNode(x){const n=document.createElement('article'),copy=document.createElement('div'),actions=document.createElement('div'),when=document.createElement('strong'),status=document.createElement('span');n.className=`lesson${x.is_past?' past':''}`;actions.className='actions';when.textContent=new Date(x.starts_at).toLocaleString([],{weekday:'long',day:'numeric',month:'long',year:'numeric',hour:'2-digit',minute:'2-digit'});status.className='badge';status.textContent=x.status==='cancelled'?'Cancelled':x.paid?'Paid':'Payment not recorded';copy.append(when,document.createElement('br'),status);if(x.can_cancel){const cancel=document.createElement('button'),move=document.createElement('button');cancel.className='secondary';cancel.textContent='Cancel';cancel.onclick=()=>openLessonModal(x.id,false);move.className='primary';move.textContent='Reschedule';move.onclick=()=>openLessonModal(x.id,true);actions.append(move,cancel)}else if(!x.is_past&&x.meeting_url&&x.status==='confirmed'){const meet=document.createElement('a');meet.className='primary';meet.href=x.meeting_url;meet.textContent='Join Meet';actions.append(meet)}n.append(copy,actions);return n}let pendingChange=null;function openLessonModal(id,reschedule){pendingChange={id,reschedule};$('#modal-title').textContent=reschedule?'Reschedule this lesson?':'Cancel this lesson?';$('#modal-message').textContent=reschedule?'This lesson will be cancelled first, then you can choose a replacement time.':'This removes the lesson from your schedule and both calendars.';$('#modal-confirm').textContent=reschedule?'Choose a new time':'Cancel lesson';$('#modal-error').textContent='';$('#lesson-modal').showModal()}function closeLessonModal(){pendingChange=null;$('#lesson-modal').close()}$('#modal-back').onclick=closeLessonModal;$('#lesson-modal').addEventListener('cancel',()=>{pendingChange=null});$('#modal-confirm').onclick=async()=>{if(!pendingChange)return;const{id,reschedule}=pendingChange;$('#modal-confirm').disabled=true;$('#modal-error').textContent='';try{const result=await api(`/api/public/scheduling/${slug}/my-lessons/${id}/cancel`,{method:'POST',body:'{}'});closeLessonModal();if(reschedule){location.href=`/book/${slug}`;return}$('#notice').textContent=result.late?'Lesson cancelled. Because it was within 12 hours, the credit was used.':'Lesson cancelled and the credit was restored.';await load()}catch(e){$('#modal-error').textContent=e.message}finally{$('#modal-confirm').disabled=false}};async function load(){const d=await api(`/api/public/scheduling/${slug}/my-lessons`);$('#identity').textContent=`Lessons with ${d.tutor_name}`;$('#account-email').textContent=d.student_email;$('#purchased').textContent=d.purchased;$('#allocated').textContent=d.allocated;$('#remaining').textContent=d.remaining;$('#lessons').replaceChildren(...d.lessons.slice().reverse().map(lessonNode))}load().catch(e=>$('#error').textContent=e.message);
</script></body></html>"""


PUBLIC_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Book a lesson</title><style>
.page-header{display:flex;justify-content:space-between;align-items:center;gap:18px}.page-header a{color:#5549b8;font-weight:750;text-decoration:none}a.primary{display:inline-flex;align-items:center;min-height:42px;background:linear-gradient(135deg,#5f72dc,#8b5bd6);color:#fff;padding:10px 18px;border-radius:11px;font-weight:750;text-decoration:none;box-shadow:0 7px 18px rgba(105,91,204,.24)}body .shell{width:min(1280px,100%)}.signin{position:relative;display:grid;grid-template-columns:minmax(0,1fr) 250px;align-items:center;min-height:360px;padding:48px 52px;background:linear-gradient(135deg,rgba(250,251,255,.98),rgba(250,246,255,.98))}.signin.hidden{display:none}.signin-copy{max-width:590px}.signin h2{font-size:clamp(27px,4vw,38px);margin:0 0 12px}.signin .muted{font-size:15px;line-height:1.6;margin:0 0 24px}.signin-sketch{position:relative;display:grid;place-items:center}.signin-sketch:before{content:"";position:absolute;width:235px;height:235px;border-radius:50%;background:linear-gradient(135deg,rgba(216,228,255,.72),rgba(238,216,255,.68))}.signin-sketch svg{position:relative;width:220px;opacity:.62}.selected-summary{margin-top:26px;padding:20px;border:1px solid #dedff1;border-radius:16px;background:linear-gradient(135deg,#f8faff,#fbf7ff)}.selected-summary>div:first-child{display:flex;align-items:baseline;justify-content:space-between;gap:16px}.selected-summary p{margin:0}.selected-list{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}.selected-chip{display:inline-flex;align-items:center;gap:9px;padding:8px 10px 8px 12px;border:1px solid #d5d7ef;border-radius:999px;background:#fff;color:#494566;font-size:12px;font-weight:700}.selected-chip button{width:22px;height:22px;min-height:0;padding:0;border:0;border-radius:50%;background:#eeecfb;color:#6257ac;box-shadow:none;cursor:pointer}.booking-info{display:grid;grid-template-columns:minmax(0,.8fr) minmax(0,1.4fr);gap:28px;margin-top:26px;padding:22px 0;border-top:1px solid #e4e3f2;border-bottom:1px solid #e4e3f2}.booking-info h3{margin-bottom:9px}.booking-info p{margin:0;line-height:1.65}@media(min-width:901px){body .booking-layout{grid-template-columns:230px minmax(0,1fr)}}@media(max-width:650px){.booking-info{grid-template-columns:1fr;gap:18px}.selected-summary>div:first-child{display:block}.selected-summary p{margin-top:5px}.signin{grid-template-columns:1fr;padding:36px 26px}.signin-sketch{margin-top:22px}.signin-sketch:before{width:175px;height:175px}.signin-sketch svg{width:165px}}
.page-actions{display:flex;align-items:center;gap:12px}.signed-in{display:flex;align-items:center;gap:9px;padding:6px 7px 6px 11px;border:1px solid #dedff1;border-radius:13px;background:linear-gradient(135deg,#f7f9ff,#faf6ff)}.signed-in span{max-width:220px;color:#716d87;font-size:11px;overflow:hidden;text-overflow:ellipsis}.signed-in strong{color:#494566}.signed-in form{display:flex}.signed-in button{min-height:32px;padding:6px 9px;border:1px solid #dedff1;border-radius:9px;background:#fff;color:#5549b8;font-size:11px;font-weight:750;cursor:pointer}@media(max-width:650px){.page-header{align-items:flex-start;flex-direction:column}.page-actions{width:100%;align-items:flex-start;flex-direction:column}.signed-in{width:100%;justify-content:space-between}}
body .shell.signin-mode{width:min(780px,100%)}
:root{font-family:"Google Sans",Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;color:#28243f;background:#f8f7ff;--indigo:#5f72dc;--violet:#8b5bd6;--accent:#7065dc;--accent-dark:#5549b8;--accent-soft:#efefff;--surface:#fafaff;--line:#dedff1;--muted:#716d87}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:clamp(18px,4vw,48px);background:radial-gradient(circle at 10% 5%,#e9efff 0,transparent 34%),radial-gradient(circle at 92% 8%,#f3eaff 0,transparent 31%),linear-gradient(145deg,#f8f9ff,#faf7ff)}.shell{width:min(1100px,100%);background:rgba(255,255,255,.97);border:1px solid rgba(211,213,238,.95);border-radius:26px;box-shadow:0 22px 60px rgba(75,73,145,.13);overflow:hidden;backdrop-filter:blur(12px);transition:width .25s ease}.shell.confirmed{width:min(780px,100%)}.page-header{padding:28px 34px;border-bottom:1px solid #ebeaf6}.confirmed .page-header{padding:25px 32px;background:linear-gradient(120deg,rgba(239,243,255,.78),rgba(248,239,255,.7))}h1{font-size:clamp(25px,3vw,30px);line-height:1.2;letter-spacing:-.025em;margin:0}h2{font-size:17px;line-height:1.3;letter-spacing:-.015em;margin:0}h3{font-size:15px;line-height:1.35;margin:0}.booking-layout{display:grid;grid-template-columns:260px minmax(0,1fr);min-height:470px}.lesson-panel{padding:30px 26px;border-right:1px solid #ebeaf6;background:linear-gradient(180deg,#f9faff,#faf7ff)}.step-heading{display:flex;align-items:center;gap:10px;margin-bottom:18px}.step-number{width:28px;height:28px;display:grid;place-items:center;flex:0 0 auto;border-radius:10px;background:linear-gradient(135deg,#e8eeff,#f0e8ff);color:var(--accent-dark);font-size:13px;font-weight:800}.types{display:grid;gap:10px}.type-choice{width:100%;text-align:left;min-height:44px;border:1px solid #d9d9ee;background:#fff;color:#494566;border-radius:13px;padding:11px 13px;cursor:pointer;font-weight:700;font-size:13px;transition:.14s}.type-choice.active,.type-choice:hover{background:linear-gradient(135deg,#eef2ff,#f1eaff);border-color:#bfc3ec;color:var(--accent-dark);box-shadow:0 5px 14px rgba(95,114,220,.12)}.scheduler{padding:30px 32px}.calendar-layout{display:grid;grid-template-columns:minmax(330px,1fr) 220px;gap:28px}.calendar-panel{min-width:0}.calendar-toolbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}.calendar-nav{display:flex;gap:7px}.icon-button{width:36px;height:36px;border:1px solid var(--line);border-radius:50%;background:#fff;color:#514d76;display:grid;place-items:center;cursor:pointer;font-size:20px;line-height:1}.icon-button:hover:not(:disabled){background:linear-gradient(135deg,#eef2ff,#f2ebff);border-color:#c5c6ed}.icon-button:disabled{opacity:.3;cursor:default}.calendar{display:grid;grid-template-columns:repeat(7,minmax(36px,1fr));gap:6px}.weekday{text-align:center;color:#817d98;font-size:11px;font-weight:750;text-transform:uppercase;padding:6px 0}.calendar-blank{aspect-ratio:1}.day{width:100%;aspect-ratio:1;border:0;border-radius:50%;background:transparent;color:#9995a8;font-size:13px;font-weight:650;cursor:default}.day.available{background:linear-gradient(135deg,#edf2ff,#f2ebff);color:#4e4975;cursor:pointer}.day.available:hover{background:linear-gradient(135deg,#dfe7ff,#e8dcff);color:var(--accent-dark)}.day.selected{background:linear-gradient(135deg,var(--indigo),var(--violet));color:#fff;box-shadow:0 6px 16px rgba(105,91,204,.28)}.day.today:not(.selected){box-shadow:inset 0 0 0 1px #98a5e3}.time-panel{border-left:1px solid #ebeaf6;padding-left:24px;min-width:0}.time-panel .muted{font-size:12px;margin:6px 0 18px}.time-slots{display:grid;gap:8px;max-height:350px;overflow:auto;padding-right:4px}.time-placeholder{color:#817d98;font-size:13px;line-height:1.5;padding:12px 0}.time-choice{width:100%;min-height:40px;border:1px solid #d6d7ed;background:#fff;color:#494566;border-radius:11px;padding:9px 12px;cursor:pointer;font-size:13px;font-weight:750;transition:.14s}.time-choice:hover,.time-choice.active{background:linear-gradient(135deg,var(--indigo),var(--violet));border-color:transparent;color:#fff;box-shadow:0 6px 16px rgba(105,91,204,.24)}.details{margin-top:26px;border-top:1px solid #ebeaf6;padding-top:24px}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}.form-grid .full{grid-column:1/-1}label{display:grid;gap:7px;margin:12px 0;font-weight:650;font-size:13px}input,textarea,button{font:inherit}input,textarea{min-height:44px;padding:10px 12px;border:1px solid #d6d7ed;border-radius:11px;outline:none;background:#fff}textarea{min-height:82px;resize:vertical}input:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(112,101,220,.14)}button.primary{min-height:42px;border:0;background:linear-gradient(135deg,var(--indigo),var(--violet));color:#fff;padding:10px 18px;border-radius:11px;font-weight:750;cursor:pointer;box-shadow:0 7px 18px rgba(105,91,204,.24)}button.primary:hover{filter:brightness(.94)}.hidden{display:none}.error{color:#a6405d;margin:14px 0 0;font-size:13px}.success{position:relative;display:grid;grid-template-columns:minmax(0,1fr) 230px;align-items:center;min-height:330px;overflow:hidden;padding:46px 44px;background:linear-gradient(135deg,rgba(250,251,255,.98),rgba(250,246,255,.98))}.success.hidden{display:none}.success-copy{position:relative;z-index:1}.success-kicker{display:inline-flex;align-items:center;gap:8px;margin-bottom:18px;color:#584bb1;font-size:13px;font-weight:800;letter-spacing:.02em;text-transform:uppercase}.success-check{width:28px;height:28px;display:grid;place-items:center;border-radius:10px;background:linear-gradient(135deg,#dfe8ff,#eadcff);box-shadow:0 5px 14px rgba(105,91,204,.14)}.success-check svg{width:16px;height:16px}.success h2{font-size:clamp(28px,4vw,38px);line-height:1.08;letter-spacing:-.035em;margin:0 0 13px}.success-time{color:#4f4868;font-size:17px;font-weight:750;line-height:1.45;margin:0}.success-meta{color:var(--muted);font-size:14px;line-height:1.55;margin:10px 0 0}.success-note{display:flex;gap:10px;align-items:flex-start;margin:26px 0 0;padding-top:20px;border-top:1px solid #e5e2f1;color:#625b75;font-size:13px;line-height:1.5}.success-note svg{width:18px;flex:0 0 auto;color:#6d63ca;margin-top:1px}.success-sketch{position:relative;z-index:0;display:grid;place-items:center}.success-sketch:before{content:"";position:absolute;width:230px;height:230px;border-radius:50%;background:linear-gradient(135deg,rgba(216,228,255,.68),rgba(238,216,255,.65));filter:blur(1px)}.success-sketch svg{position:relative;width:230px;max-width:100%;opacity:.58;filter:drop-shadow(0 12px 18px rgba(91,82,157,.09))}.hp{position:absolute;left:-9999px}@media(max-width:900px){body{place-items:start center}.booking-layout{grid-template-columns:1fr}.lesson-panel{border-right:0;border-bottom:1px solid #ebeaf6;padding:22px 26px}.types{display:flex;flex-wrap:wrap}.type-choice{width:auto}.scheduler{padding:26px}.calendar-layout{grid-template-columns:minmax(300px,1fr) 200px;gap:22px}.time-panel{padding-left:20px}}@media(max-width:650px){body{padding:0}.shell{border-radius:0;border-left:0;border-right:0;min-height:100vh}.page-header{padding:22px}.confirmed .page-header{padding:22px}.scheduler{padding:22px}.calendar-layout{grid-template-columns:1fr}.time-panel{border-left:0;border-top:1px solid #ebeaf6;padding:20px 0 0}.time-slots{grid-template-columns:repeat(2,1fr);max-height:none}.form-grid{grid-template-columns:1fr}.form-grid .full{grid-column:auto}.success{grid-template-columns:1fr;min-height:0;padding:38px 26px 30px}.success-sketch{margin:18px auto -18px}.success-sketch:before{width:180px;height:180px}.success-sketch svg{width:180px}.success-note{margin-top:20px}}
</style></head><body><main class="shell"><header class="page-header"><h1 id="title">Book a lesson</h1><a id="my-lessons" href="/book/__BOOKING_SLUG__/lessons">My lessons</a></header><section id="login" class="signin hidden"><div class="signin-copy"><div class="success-kicker">Welcome</div><h2>Sign in to choose your lessons.</h2><p class="muted">Google sign-in keeps your lessons and permanent Meet link together.</p><a class="primary" href="/auth/google/start?next=book:__BOOKING_SLUG__">Continue with Google</a></div><div class="signin-sketch" aria-hidden="true"><svg viewBox="0 0 240 220" fill="none"><defs><linearGradient id="signin-line" x1="36" y1="24" x2="206" y2="202" gradientUnits="userSpaceOnUse"><stop stop-color="#5976df"/><stop offset="1" stop-color="#9860d7"/></linearGradient></defs><g stroke="url(#signin-line)" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><path d="M54 54c0-9 7-16 16-16h103c9 0 16 7 16 16v112c0 9-7 16-16 16H70c-9 0-16-7-16-16V54Z" fill="#fff" fill-opacity=".45"/><path d="M54 72h135M84 29v19m75-19v19M82 102h24m14 0h25m14 0h12M82 126h24m14 0h25m14 0h12M82 150h24m14 0h25"/><circle cx="175" cy="159" r="24" fill="#fff" fill-opacity=".5"/><path d="M166 159h18m-9-9v18"/></g></svg></div></section><div id="app" class="booking-layout"><aside class="lesson-panel"><div class="step-heading"><span class="step-number">1</span><h2>Choose a lesson</h2></div><div class="types" id="types"></div></aside><section class="scheduler"><div class="step-heading"><span class="step-number">2</span><h2>Choose up to 10 times</h2></div><div class="calendar-layout"><div class="calendar-panel"><div class="calendar-toolbar"><h2 id="month-label">Select a lesson</h2><div class="calendar-nav"><button class="icon-button" id="previous-month" type="button" aria-label="Previous month">‹</button><button class="icon-button" id="next-month" type="button" aria-label="Next month">›</button></div></div><div class="calendar" id="calendar"></div></div><aside class="time-panel"><h3 id="selected-date">Select a date</h3><p class="muted" id="tz"></p><p class="muted" id="selection-count">0 of 10 selected</p><div class="time-slots" id="slots"><p class="time-placeholder">Choose a lesson to view available dates.</p></div></aside></div><form id="booking" class="details hidden"><h2>Finish booking</h2><div class="form-grid"><label class="full">Anything I should know?<textarea name="notes" maxlength="2000"></textarea></label></div><label class="hp">Website<input name="website" tabindex="-1" autocomplete="off"></label><button class="primary">Confirm selected lessons</button></form><p id="error" class="error"></p></section></div><section id="success" class="success hidden" aria-live="polite"><div class="success-copy"><div class="success-kicker"><span class="success-check"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 12 4 4 8-9"/></svg></span>Lessons confirmed</div><h2>You’re all set.</h2><p id="success-time" class="success-time"></p><p id="success-meta" class="success-meta"></p><p class="success-note"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="3"/><path d="M8 3v4m8-4v4M3 10h18"/></svg><span>Your permanent Google Meet link is included in every calendar invitation.</span></p></div><div class="success-sketch" aria-hidden="true"><svg viewBox="0 0 260 250" fill="none"><defs><linearGradient id="sketch" x1="42" y1="30" x2="218" y2="223" gradientUnits="userSpaceOnUse"><stop stop-color="#5976df"/><stop offset="1" stop-color="#9860d7"/></linearGradient></defs><g stroke="url(#sketch)" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"><path d="M59 58c0-10 8-18 18-18h112c10 0 18 8 18 18v123c0 10-8 18-18 18H77c-10 0-18-8-18-18V58Z" fill="#fff" fill-opacity=".52"/><path d="M59 76h148M91 31v20m84-20v20"/><path d="M96 116h74M96 138h47" opacity=".75"/><circle cx="173" cy="158" r="24" fill="#fff" fill-opacity=".55"/><path d="m161 158 8 8 16-18"/></g></svg></div></section></main>
<script>
const slug='__BOOKING_SLUG__',signedIn=__STUDENT_SIGNED_IN__,$=s=>document.querySelector(s),timezone=Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';
let profile,type,calendarMonth,selectedDayKey;let selectedStarts=[],slotsByDay=new Map(),loadedMonths=new Set();
const pad=n=>String(n).padStart(2,'0'),dayKey=d=>`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`,startOfDay=d=>new Date(d.getFullYear(),d.getMonth(),d.getDate()),monthKey=d=>`${d.getFullYear()}-${pad(d.getMonth()+1)}`;
async function api(u,o={}){const r=await fetch(u,{...o,headers:{'Content-Type':'application/json'}});if(!r.ok){let m='Request failed';try{m=(await r.json()).detail||m}catch{}throw Error(m)}return r.json()}
function typeButton(item){const b=document.createElement('button');b.type='button';b.className='type-choice';b.textContent=`${item.name} · ${item.duration_minutes} min`;b.onclick=()=>chooseType(item,b);return b}
function timeButton(date){const b=document.createElement('button'),key=date.toISOString();b.type='button';b.className='time-choice';if(selectedStarts.some(x=>x.toISOString()===key))b.classList.add('active');b.textContent=date.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});b.onclick=()=>chooseSlot(date);return b}
function latestBookingDay(){const d=startOfDay(new Date());d.setDate(d.getDate()+profile.booking_window_days);return d}
function canShowMonth(month){const first=new Date(month.getFullYear(),month.getMonth(),1),today=new Date();return first<=new Date(latestBookingDay().getFullYear(),latestBookingDay().getMonth(),1)&&new Date(first.getFullYear(),first.getMonth()+1,0)>=startOfDay(today)}
function renderTimes(){const container=$('#slots'),dates=selectedDayKey?(slotsByDay.get(selectedDayKey)||[]):[];if(!selectedDayKey){$('#selected-date').textContent='Select a date';container.replaceChildren(Object.assign(document.createElement('p'),{className:'time-placeholder',textContent:'Choose an available date from the calendar.'}));return}const selected=dates[0]||new Date(`${selectedDayKey}T12:00:00`);$('#selected-date').textContent=selected.toLocaleDateString([],{weekday:'long',month:'long',day:'numeric'});container.replaceChildren(...dates.map(timeButton))}
function renderSelection(){const section=$('#selected-summary'),list=$('#selected-list');section.classList.toggle('hidden',!selectedStarts.length);const dates=selectedStarts.slice().sort((a,b)=>a-b);list.replaceChildren(...dates.map(date=>{const chip=document.createElement('span'),remove=document.createElement('button');chip.className='selected-chip';chip.append(document.createTextNode(date.toLocaleString([],{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})));remove.type='button';remove.setAttribute('aria-label',`Remove ${date.toLocaleString()}`);remove.textContent='×';remove.onclick=()=>chooseSlot(date);chip.append(remove);return chip}))}
function renderCalendar(){const calendar=$('#calendar'),year=calendarMonth.getFullYear(),month=calendarMonth.getMonth(),first=new Date(year,month,1),last=new Date(year,month+1,0),today=startOfDay(new Date()),latest=latestBookingDay(),nodes=[];$('#month-label').textContent=first.toLocaleDateString([],{month:'long',year:'numeric'});for(const name of ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'])nodes.push(Object.assign(document.createElement('div'),{className:'weekday',textContent:name}));for(let i=0;i<(first.getDay()+6)%7;i++)nodes.push(Object.assign(document.createElement('span'),{className:'calendar-blank'}));const available=[];for(let day=1;day<=last.getDate();day++){const date=new Date(year,month,day),key=dayKey(date),has=(slotsByDay.get(key)||[]).length>0,b=document.createElement('button');b.type='button';b.className='day';b.textContent=String(day);b.disabled=!has;b.setAttribute('aria-label',date.toLocaleDateString([],{weekday:'long',month:'long',day:'numeric'}));if(has){available.push(key);b.classList.add('available');b.onclick=()=>selectDay(key)}if(key===dayKey(today))b.classList.add('today');if(key===selectedDayKey)b.classList.add('selected');if(date<today||date>latest)b.disabled=true;nodes.push(b)}if(!selectedDayKey||!selectedDayKey.startsWith(monthKey(calendarMonth)))selectedDayKey=available[0]||null;calendar.replaceChildren(...nodes);if(selectedDayKey){const active=[...calendar.querySelectorAll('.day')].find(b=>b.getAttribute('aria-label')===new Date(`${selectedDayKey}T12:00:00`).toLocaleDateString([],{weekday:'long',month:'long',day:'numeric'}));active?.classList.add('selected')}$('#previous-month').disabled=!canShowMonth(new Date(year,month-1,1));$('#next-month').disabled=!canShowMonth(new Date(year,month+1,1));renderTimes()}
async function loadMonth(){const key=monthKey(calendarMonth);if(loadedMonths.has(key)){renderCalendar();return}const today=startOfDay(new Date()),latest=latestBookingDay(),first=new Date(calendarMonth.getFullYear(),calendarMonth.getMonth(),1),last=new Date(calendarMonth.getFullYear(),calendarMonth.getMonth()+1,0),rangeStart=first<today?today:first,rangeEnd=last>latest?latest:last;$('#calendar').replaceChildren(Object.assign(document.createElement('p'),{className:'time-placeholder',textContent:'Loading availability…'}));if(rangeEnd>=rangeStart){const data=await api(`/api/public/scheduling/${slug}/slots?lesson_type_id=${type.id}&start=${dayKey(rangeStart)}&end=${dayKey(rangeEnd)}`);for(const value of data.slots){const date=new Date(value),key=dayKey(date),group=slotsByDay.get(key)||[];group.push(date);slotsByDay.set(key,group)}}loadedMonths.add(key);renderCalendar()}
function selectDay(key){selectedDayKey=key;renderCalendar()}
function chooseSlot(date){const key=date.toISOString(),index=selectedStarts.findIndex(x=>x.toISOString()===key);if(index>=0)selectedStarts.splice(index,1);else if(selectedStarts.length<10)selectedStarts.push(date);else{$('#error').textContent='You can book up to 10 lessons at once.';return}$('#selection-count').textContent=`${selectedStarts.length} of 10 selected`;$('#booking').classList.toggle('hidden',!selectedStarts.length);renderTimes();renderSelection()}
async function chooseType(item,button){type=item;selectedStarts=[];selectedDayKey=null;slotsByDay=new Map();loadedMonths=new Set();calendarMonth=new Date(new Date().getFullYear(),new Date().getMonth(),1);[...$('#types').children].forEach(n=>n.classList.remove('active'));button.classList.add('active');$('#booking').classList.add('hidden');$('#selection-count').textContent='0 of 10 selected';renderSelection();$('#error').textContent='';await loadMonth()}
async function changeMonth(offset){const next=new Date(calendarMonth.getFullYear(),calendarMonth.getMonth()+offset,1);if(!canShowMonth(next))return;calendarMonth=next;selectedDayKey=null;await loadMonth()}
async function load(){profile=await api(`/api/public/scheduling/${slug}`);$('#title').textContent=`Book a lesson with ${profile.display_name}`;if(!signedIn){$('#app').classList.add('hidden');$('#my-lessons').classList.add('hidden');$('.shell').classList.add('signin-mode');$('#login').classList.remove('hidden');return}$('#tz').textContent=`Times shown in ${timezone.replaceAll('_',' ')}`;const buttons=profile.lesson_types.map(typeButton);$('#types').replaceChildren(...buttons);if(buttons.length)await chooseType(profile.lesson_types[0],buttons[0]);else $('#error').textContent='No lesson types are currently available.'}
$('#previous-month').onclick=()=>changeMonth(-1);$('#next-month').onclick=()=>changeMonth(1);$('#booking').onsubmit=async e=>{e.preventDefault();$('#error').textContent='';const d=Object.fromEntries(new FormData(e.currentTarget));Object.assign(d,{lesson_type_id:type.id,starts_at:selectedStarts.map(x=>x.toISOString()),student_timezone:timezone});try{const result=await api(`/api/public/scheduling/${slug}/book`,{method:'POST',body:JSON.stringify(d)}),first=new Date(result.bookings[0].starts_at);$('#app').classList.add('hidden');$('.shell').classList.add('confirmed');$('#title').textContent=result.count===1?'Your lesson is booked':'Your lessons are booked';$('#success-time').textContent=result.count===1?first.toLocaleString():`${result.count} lessons booked`;$('#success-meta').textContent=`${type.name} · ${type.duration_minutes} minutes · ${timezone.replaceAll('_',' ')}`;$('#success').classList.remove('hidden')}catch(err){$('#error').textContent=err.message}};load().catch(e=>$('#error').textContent=e.message);
</script></body></html>"""
