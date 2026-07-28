import html
import logging
import re
from collections import defaultdict, deque
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    CalendarConnection,
    CalendarEventCache,
    CalendarProvider,
    HiddenSchedulingStudent,
    ICalFeed,
    LessonBooking,
    LessonPaymentAllocation,
    LessonType,
    SchedulingCalendar,
    SchedulingProfile,
    StudentMeeting,
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
from app.services.scheduling_feedback import (
    FeedbackConfigurationError,
    FeedbackDeliveryError,
    SchedulingFeedbackService,
    TurnstileValidationError,
    normalize_feedback_text,
    scheduling_hostname,
)
from app.services.scheduling_metrics import monthly_scheduling_metrics
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
logger = logging.getLogger(__name__)

COFFEE_QR_PATH = Path(__file__).resolve().parents[1] / "coffee-qr.png"
_FEEDBACK_RATE_LIMIT = 5
_FEEDBACK_RATE_WINDOW_SECONDS = 15 * 60
_feedback_attempts: dict[str, deque[float]] = defaultdict(deque)


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
    country: str = Field(min_length=2, max_length=100)
    tutoring_subjects: str = Field(min_length=2, max_length=500)


class PricingRequest(BaseModel):
    currency: str = Field(min_length=3, max_length=3)
    hourly_rate_cents: int = Field(ge=0, le=100_000_000)
    cancellation_notice_hours: int = Field(ge=0, le=720)
    late_cancellation_consumes_credit: bool = True
    cancellation_policy_text: str | None = Field(default=None, max_length=2000)


class PackageItemRequest(BaseModel):
    lesson_count: int = Field(ge=2, le=100)
    price_cents: int = Field(ge=0, le=100_000_000)
    is_active: bool = True


class PackageListRequest(BaseModel):
    packages: list[PackageItemRequest] = Field(max_length=20)


class TutorRegistrationRequest(BaseModel):
    country: str = Field(min_length=2, max_length=100)
    tutoring_subjects: str = Field(min_length=2, max_length=500)
    timezone: str = Field(min_length=1, max_length=64)


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
    student_name: str | None = Field(default=None, max_length=255)
    student_email: str | None = Field(default=None, max_length=320)
    notes: str | None = Field(default=None, max_length=2000)
    website: str | None = Field(default=None, max_length=200)  # Honeypot.


class StudentPaymentRequest(BaseModel):
    student_email: str = Field(min_length=3, max_length=320)
    lessons_purchased: int = Field(ge=1, le=100)
    amount_cents: int | None = Field(default=None, ge=0, le=10_000_000)
    booking_ids: list[UUID] = Field(default_factory=list, max_length=100)


class BugReportRequest(BaseModel):
    section: Literal["welcome", "setup", "lessons", "other"]
    message: str = Field(min_length=20, max_length=4000)
    turnstile_token: str = Field(min_length=1, max_length=2048)
    website: str | None = Field(default=None, max_length=200)


def _enforce_feedback_rate_limit(key: str, *, now: float | None = None) -> None:
    current = monotonic() if now is None else now
    attempts = _feedback_attempts[key]
    cutoff = current - _FEEDBACK_RATE_WINDOW_SECONDS
    while attempts and attempts[0] <= cutoff:
        attempts.popleft()
    if len(attempts) >= _FEEDBACK_RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Too many reports were submitted. Please try again in a few minutes.",
        )
    attempts.append(current)


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


def _superadmin_emails() -> set[str]:
    settings = get_settings()
    configured = settings.scheduling_superadmin_emails or ""
    emails = {item.strip().casefold() for item in configured.split(",") if item.strip()}
    if settings.scheduling_feedback_to_email:
        emails.add(settings.scheduling_feedback_to_email.strip().casefold())
    return emails


def _require_superadmin(request: Request) -> str:
    email = str(request.session.get("google_email") or "").strip().casefold()
    if not email or email not in _superadmin_emails():
        raise HTTPException(status_code=403, detail="Super-admin access required.")
    return email


REGISTRATION_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Create your tutor page</title><style>:root{font-family:Inter,ui-sans-serif,system-ui;color:#29213a;background:#f8f6fc}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at 10% 0,#eee8ff,transparent 35%),radial-gradient(circle at 90% 10%,#f6eafb,transparent 30%),#f8f6fc}main{width:min(620px,100%);padding:34px;background:#fff;border:1px solid #e6e0ef;border-radius:24px;box-shadow:0 20px 55px rgba(74,54,112,.12)}h1{margin:0 0 10px;font-size:32px}p{color:#756d82;line-height:1.55}label{display:grid;gap:7px;margin:18px 0;font-weight:700}input,textarea,button{font:inherit}input,textarea{width:100%;padding:12px;border:1px solid #d8d0e5;border-radius:11px}textarea{min-height:100px;resize:vertical}button{border:0;border-radius:11px;padding:12px 18px;background:#7357c7;color:#fff;font-weight:800;cursor:pointer}.error{color:#a6404a}</style></head><body><main><h1>Tell us a little about your tutoring</h1><p>This creates your free scheduling page. You can edit your public name, prices, packages, availability, and cancellation policy next.</p><form id="registration"><label>Country<input name="country" maxlength="100" autocomplete="country-name" placeholder="Portugal" required></label><label>What do you tutor?<textarea name="tutoring_subjects" maxlength="500" placeholder="For example: English, maths, piano…" required></textarea></label><input name="timezone" type="hidden"><button>Create my tutor page</button></form><p class="error" id="error" role="alert"></p></main><script>const form=document.querySelector('#registration'),error=document.querySelector('#error');form.elements.timezone.value=Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';form.onsubmit=async event=>{event.preventDefault();error.textContent='';const button=form.querySelector('button');button.disabled=true;try{const payload=Object.fromEntries(new FormData(form));const response=await fetch('/api/scheduling/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(!response.ok){let message='Registration failed';try{message=(await response.json()).detail||message}catch{}throw Error(message)}location.href='/schedule/manage#welcome'}catch(exc){error.textContent=exc.message}finally{button.disabled=false}};</script></body></html>"""


ADMIN_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Scheduling service overview</title><style>:root{font-family:Inter,ui-sans-serif,system-ui;color:#29213a;background:#f8f6fc}*{box-sizing:border-box}body{margin:0;padding:32px;background:#f8f6fc}main{max-width:1050px;margin:auto}header{display:flex;justify-content:space-between;align-items:center;gap:20px}a{color:#6042b3;font-weight:750}.metrics,.groups{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-top:24px}.card{padding:22px;background:#fff;border:1px solid #e6e0ef;border-radius:18px}.metric strong{display:block;font-size:34px}.metric span,p{color:#756d82}.list{display:grid;gap:9px}.row{display:flex;justify-content:space-between;gap:15px;padding-bottom:9px;border-bottom:1px solid #eee9f4}.row:last-child{border:0}@media(max-width:700px){.metrics,.groups{grid-template-columns:1fr}}</style></head><body><main><header><div><h1>Scheduling service overview</h1><p>Aggregate registration statistics only.</p></div><a href="/schedule/manage">Tutor dashboard</a></header><section class="metrics"><div class="card metric"><strong id="total">—</strong><span>Registered tutors</span></div><div class="card metric"><strong id="active">—</strong><span>Active booking pages</span></div><div class="card metric"><strong id="new">—</strong><span>New this month</span></div></section><section class="groups"><div class="card"><h2>Countries</h2><div class="list" id="countries"></div></div><div class="card"><h2>What people tutor</h2><div class="list" id="subjects"></div></div></section><p class="error" id="error"></p></main><script>const row=item=>{const node=document.createElement('div');node.className='row';const label=document.createElement('span'),count=document.createElement('strong');label.textContent=item.label;count.textContent=item.count;node.append(label,count);return node};fetch('/api/scheduling/admin/stats').then(async response=>{if(!response.ok)throw Error((await response.json()).detail||'Could not load statistics');return response.json()}).then(data=>{document.querySelector('#total').textContent=data.total_tutors;document.querySelector('#active').textContent=data.active_booking_pages;document.querySelector('#new').textContent=data.new_this_month;document.querySelector('#countries').replaceChildren(...data.countries.map(row));document.querySelector('#subjects').replaceChildren(...data.subjects.map(row))}).catch(error=>document.querySelector('#error').textContent=error.message);</script></body></html>"""


@router.get("/schedule/register", response_class=HTMLResponse)
async def tutor_registration_page(request: Request):
    if request.session.get("google_email"):
        return RedirectResponse("/schedule/manage", status_code=303)
    if not request.session.get("pending_tutor_email"):
        return RedirectResponse("/auth/google/start?next=scheduling", status_code=303)
    return HTMLResponse(REGISTRATION_HTML)


@router.post("/api/scheduling/register", status_code=201)
async def register_tutor(
    request: Request, payload: TutorRegistrationRequest
) -> dict[str, bool]:
    _require_same_origin(request)
    email = str(request.session.get("pending_tutor_email") or "").strip().casefold()
    name = str(request.session.get("pending_tutor_name") or "").strip() or email.split("@", 1)[0]
    if not _email_is_valid(email):
        raise HTTPException(status_code=401, detail="Start again with Google sign-in.")
    try:
        timezone = validate_timezone(payload.timezone)
    except SchedulingValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    country = " ".join(payload.country.split())
    subjects = " ".join(payload.tutoring_subjects.split())
    async with async_session_factory() as session:
        users = UserRepository(session)
        existing = await users.get_by_google_email(google_email=email)
        try:
            user = existing or await users.create_scheduling_user(
                google_email=email,
                display_name=name,
                timezone=timezone,
                commit=False,
            )
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="This Google account is already registered.") from exc
        household = await HouseholdRepository(session).ensure_household_for_user(
            user=user,
            commit=False,
        )
        profile = await SchedulingService(session).ensure_profile(
            user_id=user.id,
            household_id=household.id,
            display_name=name,
            timezone=timezone,
            commit=False,
        )
        profile.country = country
        profile.tutoring_subjects = subjects
        await session.commit()
    request.session.clear()
    request.session["google_email"] = email
    return {"registered": True}


@router.get("/schedule/admin", response_class=HTMLResponse)
async def scheduling_admin_page(request: Request) -> HTMLResponse:
    _require_superadmin(request)
    return HTMLResponse(ADMIN_HTML)


@router.get("/api/scheduling/admin/stats")
async def scheduling_admin_stats(request: Request) -> dict[str, object]:
    _require_superadmin(request)
    now = datetime.now(UTC)
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    async with async_session_factory() as session:
        profiles = list((await session.execute(select(SchedulingProfile))).scalars().all())
    countries: dict[str, int] = defaultdict(int)
    subjects: dict[str, int] = defaultdict(int)
    for profile in profiles:
        countries[profile.country or "Not provided"] += 1
        subjects[profile.tutoring_subjects or "Not provided"] += 1
    def grouped(values: dict[str, int]) -> list[dict[str, object]]:
        return [
            {"label": label, "count": count}
            for label, count in sorted(
                values.items(), key=lambda item: (-item[1], item[0].casefold())
            )
        ]
    return {
        "total_tutors": len(profiles),
        "active_booking_pages": sum(profile.is_active for profile in profiles),
        "new_this_month": sum(profile.created_at >= month_start for profile in profiles),
        "countries": grouped(countries),
        "subjects": grouped(subjects),
    }


def _lesson_type_json(item: LessonType) -> dict[str, object]:
    return {
        "id": str(item.id),
        "name": item.name,
        "description": item.description,
        "duration_minutes": item.duration_minutes,
        "location": item.location,
        "is_active": item.is_active,
    }


def _management_booking_json(
    item: LessonBooking, *, now: datetime
) -> dict[str, object]:
    category = (
        "cancelled"
        if item.status == "cancelled"
        else "completed"
        if item.ends_at <= now
        else "upcoming"
    )
    return {
        "id": str(item.id),
        "student_name": item.student_name,
        "student_email": item.student_email,
        "starts_at": item.starts_at.isoformat(),
        "ends_at": item.ends_at.isoformat(),
        "status": item.status,
        "category": category,
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
    settings = get_settings()
    feedback_service = SchedulingFeedbackService(settings)
    page = MANAGEMENT_HTML.replace(
        "__TURNSTILE_SITE_KEY__",
        html.escape(settings.turnstile_site_key or "", quote=True),
    ).replace(
        "__FEEDBACK_CONFIGURED__",
        "true" if feedback_service.configured else "false",
    ).replace(
        "__ADMIN_LINK__",
        '<a class="admin-link" href="/schedule/admin">Service overview</a>'
        if str(request.session.get("google_email") or "").strip().casefold()
        in _superadmin_emails()
        else "",
    )
    return HTMLResponse(page)


@router.get("/api/scheduling/assets/coffee-qr.png", response_class=FileResponse)
async def scheduling_coffee_qr(request: Request) -> FileResponse:
    async with async_session_factory() as session:
        await _management_profile(request, session)
    return FileResponse(
        COFFEE_QR_PATH,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("/api/scheduling/bug-reports", status_code=202)
async def submit_scheduling_bug_report(
    request: Request,
    payload: BugReportRequest,
) -> dict[str, bool]:
    _require_same_origin(request)
    async with async_session_factory() as session:
        user, _, _ = await _management_profile(request, session)

    if payload.website:
        return {"sent": True}
    message = normalize_feedback_text(payload.message)
    if len(message) < 20:
        raise HTTPException(status_code=422, detail="Please add a little more detail.")

    reporter_email = str(user.google_email or "").strip().casefold()
    if not _email_is_valid(reporter_email):
        raise HTTPException(status_code=401, detail="A verified Google email is required.")
    _enforce_feedback_rate_limit(reporter_email)

    settings = get_settings()
    service = SchedulingFeedbackService(settings)
    if not service.configured:
        raise HTTPException(status_code=503, detail="Bug reporting is not configured yet.")
    try:
        await service.verify_turnstile(
            token=payload.turnstile_token,
            remote_ip=request.client.host if request.client else None,
            expected_hostname=scheduling_hostname(settings),
        )
        await service.send_bug_report(
            reporter_email=reporter_email,
            section=payload.section,
            message=message,
        )
    except TurnstileValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FeedbackConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Bug reporting is not configured yet.") from exc
    except FeedbackDeliveryError as exc:
        logger.exception("Tutor scheduling bug report delivery failed")
        raise HTTPException(
            status_code=503,
            detail="The report could not be sent. Please try again shortly.",
        ) from exc
    return {"sent": True}


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
        all_allocations = list(allocation_result.scalars().all())
        allocations = {item.booking_id: item.payment_id for item in all_allocations}
        hidden_result = await session.execute(
            select(HiddenSchedulingStudent.student_email).where(
                HiddenSchedulingStudent.profile_id == profile.id
            )
        )
        hidden_student_emails = set(hidden_result.scalars().all())
        student_emails = sorted(
            (
                {item.student_email for item in all_bookings}
                | {item.student_email for item in all_payments}
            )
            - hidden_student_emails
        )
        now = datetime.now(UTC)
        metrics = monthly_scheduling_metrics(
            bookings=all_bookings,
            payments=all_payments,
            allocations=all_allocations,
            timezone=profile.timezone,
            now=now,
        )
        settings = get_settings()
        public_base = (settings.scheduling_public_base_url or settings.public_base_url).rstrip("/")
        return {
            "profile": {
                "display_name": profile.display_name,
                "country": profile.country or "",
                "tutoring_subjects": profile.tutoring_subjects or "",
                "slug": profile.slug,
                "timezone": profile.timezone,
                "minimum_notice_minutes": profile.minimum_notice_minutes,
                "booking_window_days": profile.booking_window_days,
                "buffer_before_minutes": profile.buffer_before_minutes,
                "buffer_after_minutes": profile.buffer_after_minutes,
                "slot_interval_minutes": profile.slot_interval_minutes,
                "booking_calendar_id": profile.booking_calendar_id,
                "is_active": profile.is_active,
                "currency": profile.currency,
                "hourly_rate_cents": profile.hourly_rate_cents,
                "cancellation_notice_hours": profile.cancellation_notice_hours,
                "late_cancellation_consumes_credit": profile.late_cancellation_consumes_credit,
                "cancellation_policy_text": profile.cancellation_policy_text or "",
                "public_url": f"{public_base}/book/{profile.slug}",
            },
            "lesson_types": [
                _lesson_type_json(item)
                for item in await repository.list_lesson_types(profile_id=profile.id)
            ],
            "packages": [
                {
                    "lesson_count": item.lesson_count,
                    "price_cents": item.price_cents,
                    "is_active": item.is_active,
                }
                for item in await repository.list_packages(profile_id=profile.id)
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
                _management_booking_json(item, now=now) for item in all_bookings
            ],
            "metrics": {
                "completed_lessons": metrics.completed_lessons,
                "total_lessons": metrics.total_lessons,
                "earned_cents": metrics.earned_cents,
                "projected_cents": metrics.projected_cents,
            },
            "payments": [
                {
                    "id": str(item.id),
                    "student_email": item.student_email,
                    "lessons_purchased": item.lessons_purchased,
                    "amount_cents": item.amount_cents,
                    "currency": item.currency,
                    "paid_at": item.paid_at.isoformat(),
                    "allocated": sum(
                        allocation.payment_id == item.id for allocation in all_allocations
                    ),
                }
                for item in reversed(all_payments)
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
        profile.country = " ".join(payload.country.split())
        profile.tutoring_subjects = " ".join(payload.tutoring_subjects.split())
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


@router.put("/api/scheduling/pricing")
async def update_scheduling_pricing(
    request: Request, payload: PricingRequest
) -> dict[str, bool]:
    _require_same_origin(request)
    currency = payload.currency.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise HTTPException(status_code=400, detail="Currency must be a three-letter code.")
    async with async_session_factory() as session:
        _, _, profile = await _management_profile(request, session)
        if profile.currency != currency:
            payment_count = await session.scalar(
                select(func.count(StudentPayment.id)).where(
                    StudentPayment.profile_id == profile.id
                )
            )
            if payment_count:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Currency cannot be changed after payments are registered, "
                        "because historical amounts are not exchange-rate converted."
                    ),
                )
        profile.currency = currency
        profile.hourly_rate_cents = payload.hourly_rate_cents
        profile.cancellation_notice_hours = payload.cancellation_notice_hours
        profile.late_cancellation_consumes_credit = payload.late_cancellation_consumes_credit
        profile.cancellation_policy_text = (
            (payload.cancellation_policy_text or "").strip() or None
        )
        await session.commit()
    return {"saved": True}


@router.put("/api/scheduling/packages")
async def replace_scheduling_packages(
    request: Request, payload: PackageListRequest
) -> dict[str, object]:
    _require_same_origin(request)
    lesson_counts = [item.lesson_count for item in payload.packages]
    if len(lesson_counts) != len(set(lesson_counts)):
        raise HTTPException(status_code=400, detail="Each package size must be unique.")
    async with async_session_factory() as session:
        _, _, profile = await _management_profile(request, session)
        packages = await SchedulingRepository(session).replace_packages(
            profile_id=profile.id,
            packages=[
                (item.lesson_count, item.price_cents, item.is_active)
                for item in payload.packages
            ],
        )
    return {"saved": True, "count": len(packages)}


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
            currency=profile.currency,
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


@router.delete("/api/scheduling/student-payments/{payment_id}")
async def delete_student_payment(request: Request, payment_id: UUID) -> dict[str, bool]:
    _require_same_origin(request)
    async with async_session_factory() as session:
        _, _, profile = await _management_profile(request, session)
        payment = await session.get(StudentPayment, payment_id)
        if payment is None or payment.profile_id != profile.id:
            raise HTTPException(status_code=404, detail="Registered payment not found.")
        await session.delete(payment)
        await session.commit()
        return {"deleted": True}


@router.delete("/api/scheduling/students/{student_email}")
async def remove_scheduling_student(request: Request, student_email: str) -> dict[str, bool]:
    _require_same_origin(request)
    email = student_email.strip().casefold()
    if not _email_is_valid(email):
        raise HTTPException(status_code=400, detail="Enter a valid student email address.")
    async with async_session_factory() as session:
        _, _, profile = await _management_profile(request, session)
        booking_count = await session.scalar(
            select(func.count(LessonBooking.id)).where(
                LessonBooking.profile_id == profile.id,
                LessonBooking.student_email == email,
            )
        )
        payment_count = await session.scalar(
            select(func.count(StudentPayment.id)).where(
                StudentPayment.profile_id == profile.id,
                StudentPayment.student_email == email,
            )
        )
        if not booking_count and not payment_count:
            await session.execute(
                delete(StudentMeeting).where(
                    StudentMeeting.profile_id == profile.id,
                    StudentMeeting.student_email == email,
                )
            )
            await session.execute(
                delete(HiddenSchedulingStudent).where(
                    HiddenSchedulingStudent.profile_id == profile.id,
                    HiddenSchedulingStudent.student_email == email,
                )
            )
            await session.commit()
            return {"deleted": True, "hidden": False}
        existing = await session.scalar(
            select(HiddenSchedulingStudent).where(
                HiddenSchedulingStudent.profile_id == profile.id,
                HiddenSchedulingStudent.student_email == email,
            )
        )
        if existing is None:
            session.add(
                HiddenSchedulingStudent(profile_id=profile.id, student_email=email)
            )
        await session.commit()
        return {"deleted": False, "hidden": True}


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


def _student_account_control(*, slug: str, student_email: str) -> str:
    if student_email:
        content = (
            '<div class="signed-in"><span>Signed in as '
            f'<strong id="account-email">{html.escape(student_email)}</strong></span>'
            f'<form method="post" action="/auth/student/logout?slug={slug}">'
            '<button type="submit">Sign out</button></form></div>'
        )
    else:
        content = (
            f'<a class="account-sign-in" href="/auth/google/start?next=book:{slug}">Sign in</a>'
        )
    return f'<div class="page-account" id="account">{content}</div>'


@router.get("/book/{slug}", response_class=HTMLResponse)
async def public_booking_page(request: Request, slug: str) -> HTMLResponse:
    safe_slug = html.escape(slug, quote=True)
    student_email = str(request.session.get("student_google_email") or "").strip().casefold()
    signed_in = bool(student_email)
    page = PUBLIC_HTML.replace("__ACCOUNT_CONTROL_CSS__", ACCOUNT_CONTROL_CSS).replace(
        "__BOOKING_SLUG__", safe_slug
    )
    page = page.replace(
        "<body>",
        f"<body>{_student_account_control(slug=safe_slug, student_email=student_email)}",
        1,
    )
    page = page.replace(
        '<form id="booking"',
        SELECTED_SUMMARY_HTML + PUBLIC_INFO_HTML + '<form id="booking"',
    )
    page = page.replace("__STUDENT_SIGNED_IN__", "true" if signed_in else "false")
    return HTMLResponse(page)


@router.get("/book/{slug}/lessons", response_class=HTMLResponse)
async def student_lessons_page(request: Request, slug: str):
    student_email = str(request.session.get("student_google_email") or "").strip().casefold()
    if not student_email:
        return RedirectResponse(f"/auth/google/start?next=book:{slug}", status_code=303)
    safe_slug = html.escape(slug, quote=True)
    page = STUDENT_HTML.replace("__BOOKING_SLUG__", safe_slug).replace(
        "__STUDENT_ACCOUNT__",
        _student_account_control(slug=safe_slug, student_email=student_email),
    )
    return HTMLResponse(page)


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
            "cancellation_notice_hours": profile.cancellation_notice_hours,
            "late_cancellation_consumes_credit": profile.late_cancellation_consumes_credit,
            "cancellation_policy_text": profile.cancellation_policy_text or "",
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
                    "late": item.starts_at - now
                    < timedelta(hours=profile.cancellation_notice_hours),
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
        late = booking.starts_at - now < timedelta(
            hours=profile.cancellation_notice_hours
        )
        consumes_credit = late and profile.late_cancellation_consumes_credit
        allocation = await repository.allocation_for_booking(booking_id=booking.id)
        credit_restored = allocation is not None and not consumes_credit
        if credit_restored:
            await session.delete(allocation)
        booking.status = "cancelled"
        booking.cancelled_at = now
        booking.cancellation_consumes_credit = consumes_credit
        await session.commit()
        return {
            "cancelled": True,
            "credit_restored": credit_restored,
            "late": consumes_credit,
        }


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
            "currency": profile.currency,
            "hourly_rate_cents": profile.hourly_rate_cents,
            "packages": [
                {
                    "lesson_count": item.lesson_count,
                    "price_cents": item.price_cents,
                }
                for item in await repository.list_packages(
                    profile_id=profile.id, active_only=True
                )
            ],
            "cancellation_notice_hours": profile.cancellation_notice_hours,
            "late_cancellation_consumes_credit": profile.late_cancellation_consumes_credit,
            "cancellation_policy_text": profile.cancellation_policy_text or "",
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
    if payload.website:
        raise HTTPException(status_code=400, detail="Invalid booking request.")
    signed_in = bool(request.session.get("student_google_email"))
    if signed_in:
        student_email, student_name = _student_identity(request)
    else:
        student_email = (payload.student_email or "").strip().casefold()
        student_name = (payload.student_name or "").strip()
        if not student_name:
            raise HTTPException(status_code=400, detail="Enter your name.")
        if not _email_is_valid(student_email):
            raise HTTPException(status_code=400, detail="Enter a valid email address.")
        if len(payload.starts_at) != 1:
            raise HTTPException(
                status_code=400,
                detail="Sign in with Google to book more than one lesson at a time.",
            )
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
                guest_booking=not signed_in,
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
main{max-width:1440px;margin:auto;padding:36px 28px 88px}header{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-bottom:26px}.admin-link{color:var(--purple-dark);font-weight:750;text-decoration:none}
h1{font-size:clamp(30px,4vw,46px);letter-spacing:-.035em;margin:0}h2{font-size:22px;letter-spacing:-.02em;margin:0 0 18px}.muted{color:var(--muted);line-height:1.45}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;align-items:start}.column{display:grid;gap:20px;align-content:start;min-width:0}
.card{min-width:0;background:rgba(255,255,255,.94);border:1px solid rgba(221,212,235,.9);border-radius:22px;padding:24px;box-shadow:0 12px 34px rgba(67,47,98,.08);backdrop-filter:blur(12px)}
.tabs{display:inline-flex;gap:5px;margin:0 0 24px;padding:5px;border:1px solid var(--line);border-radius:999px;background:rgba(255,255,255,.78);box-shadow:0 8px 24px rgba(67,47,98,.06)}.tab{min-width:112px;background:transparent;color:var(--muted);box-shadow:none}.tab:hover{background:var(--purple-soft);color:var(--purple-dark);box-shadow:none;transform:none}.tab.active{background:linear-gradient(135deg,#6372d8,#8559cf);color:#fff;box-shadow:0 5px 14px rgba(96,66,179,.2)}.tab-panel[hidden]{display:none}
.welcome-layout{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(300px,.55fr);gap:20px;align-items:start}.welcome-media{display:grid;gap:18px}.welcome-copy{font-size:16px;line-height:1.68}.welcome-copy p:first-of-type{font-size:18px;color:#4f435f}.coffee-card{text-align:center}.coffee-card .qr{display:block;width:min(220px,75%);height:auto;margin:0 auto 16px;border-radius:18px}.coffee-card p{margin:0 0 14px}.setup-steps{counter-reset:onboarding;display:grid;gap:10px;margin-top:18px}.setup-step{position:relative;padding:13px 14px 13px 52px;border:1px solid var(--line);border-radius:14px;background:#fefeff}.setup-step:before{counter-increment:onboarding;content:counter(onboarding);position:absolute;left:14px;top:12px;width:27px;height:27px;display:grid;place-items:center;border-radius:9px;background:var(--purple-soft);color:var(--purple-dark);font-weight:800}.setup-step strong,.setup-step span{display:block}.setup-step span{margin-top:3px;color:var(--muted);font-size:13px}.welcome-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
.overview{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:20px}.metric{padding:20px 22px;border:1px solid var(--line);border-radius:18px;background:linear-gradient(135deg,rgba(255,255,255,.97),rgba(247,244,255,.94));box-shadow:0 9px 25px rgba(67,47,98,.06)}.metric strong{display:block;font-size:30px;letter-spacing:-.03em}.metric span{color:var(--muted);font-size:13px}.lessons-grid{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(420px,.9fr);gap:20px;align-items:start}.student-tools{display:flex;gap:10px;align-items:center}.student-tools input{flex:1}.student-list{max-height:510px;overflow:auto;padding-right:4px}.student-card{width:100%;min-height:0;padding:8px;border:1px solid var(--line);border-radius:15px;background:#fefeff;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:center}.student-card.active{background:linear-gradient(135deg,#f2f5ff,#f7f0ff);border-color:#cfc6e8}.student-select{min-width:0;min-height:0;padding:5px 6px;background:transparent;color:inherit;box-shadow:none;text-align:left;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center}.student-select:hover{background:transparent;color:inherit;box-shadow:none;transform:none}.student-card strong,.student-card span{display:block}.student-card small{display:block;margin-top:4px;color:var(--muted);font-weight:500;overflow:hidden;text-overflow:ellipsis}.icon-button{width:36px;min-width:36px;min-height:36px;padding:0}.icon-button svg{width:17px;height:17px;pointer-events:none}.student-remove{min-height:36px}.balance{min-width:58px;text-align:center;padding:7px 9px;border-radius:12px;background:var(--purple-soft);color:var(--purple-dark);font-size:12px;font-weight:800}.balance b{display:block;font-size:18px}.empty{padding:18px;border:1px dashed #d8d0e5;border-radius:15px;color:var(--muted);text-align:center}.payment-card{position:sticky;top:18px}.payment-card .list{max-height:260px;overflow:auto}.payment-card h3{margin:22px 0 0;font-size:16px}.payment-card .item{align-items:flex-start}
form{display:grid;gap:14px}label{display:grid;gap:7px;font-size:13px;font-weight:650;margin:0}input,select,textarea,button{font:inherit}input,select,textarea{width:100%;min-height:44px;padding:10px 13px;border:1px solid #d9d1e4;border-radius:12px;background:#fff;color:inherit;outline:none;transition:border-color .16s,box-shadow .16s,background .16s}textarea{min-height:88px;resize:vertical}input:hover,select:hover,textarea:hover{border-color:#c4b6da}input:focus,select:focus,textarea:focus{border-color:var(--purple);box-shadow:0 0 0 3px rgba(115,87,199,.14);background:#fefeff}
button,.button{min-height:42px;border:1px solid transparent;border-radius:999px;padding:10px 17px;background:var(--purple);color:#fff;font-weight:700;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;box-shadow:0 5px 14px rgba(96,66,179,.16);transition:transform .14s,box-shadow .14s,background .14s}button:hover,.button:hover{background:var(--purple-dark);box-shadow:0 7px 18px rgba(96,66,179,.23);transform:translateY(-1px)}button:focus-visible,.button:focus-visible{outline:3px solid rgba(115,87,199,.24);outline-offset:2px}.secondary{background:var(--purple-soft);border-color:#e2d9f7;color:#584396;box-shadow:none}.secondary:hover{background:#e4dcf8;color:#4d378d}.danger{background:var(--danger-soft);border-color:#f8dfe2;color:var(--danger);box-shadow:none}.danger:hover{background:#fbe3e6;color:#92353f}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.row>button,.row>.button,form>button{margin-top:4px;justify-self:start}#profile>.row:not(label){display:grid;grid-template-columns:1fr}label.row{display:flex;align-items:center;min-height:34px}label.row input{min-height:auto;accent-color:var(--purple)}.list{display:grid;gap:10px;margin-top:16px}.item{display:flex;gap:14px;align-items:center;justify-content:space-between;border:1px solid var(--line);border-radius:15px;padding:12px 14px;background:#fefeff}.item p{margin:3px 0}.item button{min-height:36px;padding:7px 13px}.item button.icon-button{padding:0}.item-actions{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end}.lesson-filters{display:flex;gap:7px;flex-wrap:wrap;margin-top:14px}.lesson-filter{min-height:34px;padding:7px 13px;background:#fff;border-color:var(--line);color:var(--muted);box-shadow:none}.lesson-filter:hover,.lesson-filter.active{background:var(--purple-soft);border-color:#ddd3f2;color:var(--purple-dark);box-shadow:none;transform:none}.lesson-item.completed{opacity:.72}.lesson-item.cancelled{opacity:.58;border-style:dashed}.lesson-status{flex:0 0 auto;padding:5px 9px;border-radius:999px;background:var(--purple-soft);color:var(--purple-dark);font-size:12px;font-weight:750}.lesson-item.cancelled .lesson-status{background:#f1eef4;color:#777080}.lesson-item.completed .lesson-status{background:#f3f1f6;color:#6f6978}.help{font-size:12px;margin:0}.help a{color:var(--purple-dark)}#public-link{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:18px 0 0}#public-link span{min-width:0;overflow-wrap:anywhere}#copy-public-link{flex:0 0 auto}
.package-row{display:grid;grid-template-columns:minmax(70px,.7fr) minmax(90px,.8fr) minmax(100px,1fr) auto auto}.package-row:before{content:attr(data-labels);grid-column:1/-1;color:var(--muted);font-size:11px}.package-row input[type=checkbox]{width:22px;min-height:22px;accent-color:var(--purple)}
.availability-card .list{gap:10px}.days{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) 42px;gap:8px;padding:10px;border:1px solid var(--line);border-radius:16px;background:var(--purple-pale);transition:opacity .16s,background .16s}.days select{grid-column:1/3}.days input{min-width:0}.days.disabled{background:#faf9fc;opacity:.68}.days.disabled select,.days.disabled input{color:#8c8595;background:#f4f1f7}.days .day-toggle{grid-column:3;grid-row:1/3;align-self:stretch;min-height:0;width:42px;padding:0;border:1px solid #e5deed;border-radius:13px;background:#fff;color:#796b8c;box-shadow:none}.days .day-toggle:hover{background:#f8edf1;border-color:#efd9df;color:#9f4450;box-shadow:none;transform:none}.days.disabled .day-toggle{background:var(--purple-soft);border-color:#ddd3f2;color:var(--purple)}.days.disabled .day-toggle:hover{background:#e5dcf8;color:var(--purple-dark)}.days .day-toggle svg{width:18px;height:18px;pointer-events:none}.availability-card>.row{margin-top:16px}.status{position:fixed;left:50%;bottom:22px;z-index:10;min-height:0;transform:translateX(-50%);padding:10px 16px;border-radius:999px;background:#302443;color:#fff;box-shadow:0 10px 30px rgba(48,36,67,.2)}.status:empty{display:none}.hidden{position:absolute;left:-9999px}
.dashboard-footer{display:grid;grid-template-columns:minmax(260px,.7fr) minmax(360px,1.3fr);gap:20px;margin-top:26px}.support-card{background:linear-gradient(135deg,rgba(255,255,255,.97),rgba(250,244,255,.96))}.support-card h2,.feedback-card h2{margin-bottom:9px}.support-card p,.feedback-card>p{margin-top:0}.support-link{display:inline-flex;align-items:center;gap:8px;margin-top:4px;color:var(--purple-dark);font-weight:750}.feedback-card form{grid-template-columns:180px minmax(0,1fr)}.feedback-card label.message{grid-column:1/-1}.feedback-card textarea{min-height:120px}.feedback-card .turnstile-row{grid-column:1/-1;display:flex;align-items:center;gap:14px;flex-wrap:wrap}.feedback-card .feedback-note{font-size:12px}.feedback-card .hp{position:absolute;left:-9999px}.feedback-unavailable{padding:12px;border-radius:12px;background:#fff7e5;color:#725813;font-size:13px}
@media(max-width:980px){main{padding:28px 20px 72px}.grid,.overview{grid-template-columns:repeat(2,minmax(0,1fr))}.lessons-grid{grid-template-columns:1fr}.payment-card{position:static}}
@media(max-width:980px){.welcome-layout,.dashboard-footer{grid-template-columns:1fr}.feedback-card form{grid-template-columns:1fr}}
@media(max-width:680px){main{padding:22px 14px 60px}header{align-items:flex-start}.grid{grid-template-columns:1fr;gap:14px}.card{padding:20px;border-radius:18px}.days{grid-template-columns:minmax(0,1fr) minmax(0,1fr) auto}.overview{grid-template-columns:1fr}.tabs{display:flex}.tab{flex:1;min-width:0;padding-inline:10px}.student-tools{align-items:stretch;flex-direction:column}.feedback-card .turnstile-row{align-items:flex-start;flex-direction:column}.package-row{grid-template-columns:1fr 1fr}.package-row:before{grid-column:1/-1}}
</style></head><body><main><header><h1>Lesson scheduling</h1>__ADMIN_LINK__</header><nav class="tabs" aria-label="Dashboard sections"><button class="tab active" type="button" data-tab="welcome">Welcome</button><button class="tab" type="button" data-tab="setup">Setup</button><button class="tab" type="button" data-tab="lessons">Lessons</button></nav>
<section class="tab-panel" id="welcome-panel"><div class="welcome-layout"><section class="card welcome-copy"><h2>Less scheduling admin. More actual teaching.</h2><p>I built this free scheduling service while tutoring because I know the pain: too much admin, conversations scattered across different apps, and several free-tier tools held together with digital duct tape.</p><p>It helps tutors manage bookings, keep track of payments and income, and let students schedule several lessons at once. Rescheduling and cancellations are simpler too—and your cancellation policy is shown clearly during booking, so nobody has to negotiate the rules after something changes.</p><p>There’s no subscription, and the service never processes your payments. Lessons stay in your calendar and your students’ calendars, while you remain in control of payments.</p><p>There’s no lock-in either. If the service disappears in a dramatic puff of experimental software, your calendar events remain where they are—you can simply return to arranging lessons the old-fashioned way.</p><p>This is an early version that I’m sharing for free while I test and improve it. If it saves you some admin, wonderful. If you find something confusing or broken, that is useful too—please tell me.</p><h2>Get ready to accept bookings</h2><div class="setup-steps"><div class="setup-step"><strong>Connect your calendars</strong><span>Add every calendar that should block unavailable times.</span></div><div class="setup-step"><strong>Choose where lessons are created</strong><span>Select a writable Google calendar for invitations and Meet links.</span></div><div class="setup-step"><strong>Add lesson types and availability</strong><span>Set lesson lengths, teaching hours, notice, and non-lesson travel buffers.</span></div><div class="setup-step"><strong>Share your booking page</strong><span>Copy your public link when the schedule looks right.</span></div></div><div class="welcome-actions"><button type="button" id="start-setup">Start with calendars</button></div></section><aside class="welcome-media"><section class="card coffee-card"><h2>Help shape what comes next</h2><img class="qr" src="/api/scheduling/assets/coffee-qr.png" alt="QR code for Buy Me a Coffee"><p class="muted">Have a feature idea? Send it with a coffee.</p><a class="button secondary" href="https://www.buymeacoffee.com/okserm" target="_blank" rel="noopener noreferrer">Open Buy Me a Coffee</a></section></aside></div></section>
<section class="tab-panel" id="setup-panel" hidden><div class="grid"><div class="column"><section class="card"><h2>Booking page</h2><form id="profile"><label>Your public name<input name="display_name" required></label><label>Country<input name="country" maxlength="100" autocomplete="country-name" required></label><label>What do you tutor?<textarea name="tutoring_subjects" maxlength="500" required></textarea></label><label>Booking link<input name="slug" required></label><label>Timezone<input name="timezone" required></label><div class="row"><label>Minimum notice (minutes)<input name="minimum_notice_minutes" type="number" min="0"></label><label>Booking window (days)<input name="booking_window_days" type="number" min="1"></label></div><div class="row"><label>Buffer before and after non-lesson events (minutes)<input name="non_lesson_buffer_minutes" type="number" min="0" max="240" required></label><label>Start-time increments (minutes)<input name="slot_interval_minutes" type="number" min="5"><span class="muted">For example, 15 offers 09:00, 09:15, 09:30, and so on.</span></label></div><p class="muted">This protects travel or preparation time on both sides of anything in your calendar that is not a lesson. Lessons can still be booked back-to-back.</p><label>Calendar for new lessons<select name="booking_calendar_id"><option value="">Primary Google calendar</option></select></label><label class="row"><input name="is_active" type="checkbox" style="width:auto">Accept bookings</label><button>Save settings</button></form><div id="public-link"><span>Public page: <a id="public-url" target="_blank" rel="noopener"></a></span><button id="copy-public-link" class="secondary icon-button" type="button" aria-label="Copy booking link" title="Copy booking link"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><rect x="8" y="8" width="11" height="11" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg></button></div></section>
<section class="card"><h2>Google calendars</h2><p class="muted">Selected calendars block lesson availability. Reconnect if calendar discovery asks for permission.</p><div class="list" id="google-accounts"></div><div class="row"><a class="button secondary" href="/calendar/google/start?next=scheduling">Connect another account</a><button id="discover" type="button">Refresh calendars</button></div><div class="list" id="calendars"></div></section></div>
<div class="column"><section class="card availability-card"><h2>Weekly availability</h2><p class="muted">Set times for all seven days. Disable any day you do not teach. Times use your timezone.</p><div class="list" id="availability"></div><div class="row"><button id="add-window" class="secondary" type="button">Add another time</button><button id="save-availability" type="button">Save availability</button></div></section>
<section class="card"><h2>iCloud calendars</h2><p class="muted">Connect privately with an Apple app-specific password. Access is read-only and your calendars remain private.</p><form id="icloud"><label>Apple Account email<input name="account_email" type="email" autocomplete="username" required></label><label>App-specific password<input name="app_specific_password" type="password" autocomplete="new-password" required></label><p class="muted help">Use a password created for this scheduler, never your main Apple password. <a href="https://account.apple.com/account/manage/section/security" target="_blank" rel="noopener">Create one in Apple Account security</a>.</p><button>Connect iCloud</button></form><div class="list" id="icloud-accounts"></div><div class="list" id="icloud-calendars"></div></section></div>
<div class="column"><section class="card"><h2>Lesson types</h2><form id="lesson"><input name="id" type="hidden"><label>Name<input name="name" required placeholder="English lesson"></label><label>Length (minutes)<input name="duration_minutes" type="number" min="15" value="60" required></label><label>Location or call link<input name="location"></label><label>Description<textarea name="description"></textarea></label><label class="row"><input name="is_active" type="checkbox" style="width:auto" checked>Active</label><button>Save lesson type</button></form><div class="list" id="lessons"></div></section><section class="card"><h2>Pricing and cancellation</h2><form id="pricing"><div class="row"><label>Currency<input name="currency" maxlength="3" required placeholder="EUR"></label><label>Hourly fee<input name="hourly_rate" type="number" min="0" step="0.01" required></label></div><h3>Packages</h3><p class="muted">The suggested progressive discounts are editable. Remove every package if you prefer single lessons only.</p><div class="list" id="packages"></div><button class="secondary" id="add-package" type="button">Add package</button><div class="row"><label>Free cancellation notice (hours)<input name="cancellation_notice_hours" type="number" min="0" max="720" required></label><label class="row"><input name="late_cancellation_consumes_credit" type="checkbox" style="width:auto">Late cancellation uses the lesson credit</label></div><label>Additional policy text<textarea name="cancellation_policy_text" maxlength="2000" placeholder="Anything else students should know"></textarea></label><button>Save pricing and policy</button></form></section></div></div></section>
<section class="tab-panel" id="lessons-panel" hidden><div class="overview"><div class="metric"><strong id="completed-count">0</strong><span>Completed lessons this month</span></div><div class="metric"><strong id="monthly-count">0</strong><span>Total lessons this month</span></div><div class="metric"><strong id="student-count">0</strong><span>Students</span></div><div class="metric"><strong id="earned-income">€0</strong><span id="projected-income">€0 projected this month</span></div></div><div class="lessons-grid"><section class="card"><h2>All lessons</h2><p class="muted">Upcoming, completed, and cancelled lessons.</p><div class="lesson-filters" role="group" aria-label="Filter lessons"><button class="lesson-filter active" type="button" data-lesson-filter="all">All</button><button class="lesson-filter" type="button" data-lesson-filter="upcoming">Upcoming</button><button class="lesson-filter" type="button" data-lesson-filter="completed">Completed</button><button class="lesson-filter" type="button" data-lesson-filter="cancelled">Cancelled</button></div><div class="list" id="bookings"></div></section><div class="column"><section class="card"><h2>Students and balances</h2><div class="student-tools"><input id="student-search" type="search" placeholder="Search name or email" aria-label="Search students"></div><div class="list student-list" id="students"></div></section><section class="card payment-card"><h2>Register payment</h2><p class="muted">Choose a student, select a configured package or enter a custom payment, and optionally assign it to unpaid lessons.</p><form id="payment"><label>Student email<input name="student_email" type="email" list="student-emails" required></label><datalist id="student-emails"></datalist><label>Pricing option<select name="pricing_option"><option value="custom">Custom payment</option></select></label><div class="row"><label>Lessons purchased<input name="lessons_purchased" type="number" min="1" max="100" required></label><label><span id="payment-amount-label">Amount paid</span><input name="amount_euros" type="number" min="0" step="0.01"></label></div><div class="list" id="payment-lessons"></div><button>Record payment</button></form><h3>Registered payments</h3><div class="list" id="registered-payments"></div></section></div></div></section>
<footer class="dashboard-footer"><section class="card support-card"><h2>Ideas are welcome</h2><p class="muted">Have a feature request? Leave it with a coffee and help choose what I build next.</p><script type="text/javascript" src="https://cdnjs.buymeacoffee.com/1.0.0/button.prod.min.js" data-name="bmc-button" data-slug="okserm" data-color="#FFDD00" data-emoji="☕" data-font="Cookie" data-text="Buy me a coffee" data-outline-color="#000000" data-font-color="#000000" data-coffee-color="#ffffff"></script></section><section class="card feedback-card"><h2>Found a bug?</h2><p class="muted">Tell me what happened. Your verified sign-in email is included privately so I can follow up; it is never displayed here.</p><form id="bug-report"><label>Where did it happen?<select name="section"><option value="welcome">Welcome</option><option value="setup">Setup</option><option value="lessons">Lessons</option><option value="other">Somewhere else</option></select></label><label class="hp">Website<input name="website" tabindex="-1" autocomplete="off"></label><label class="message">What happened?<textarea name="message" minlength="20" maxlength="4000" required placeholder="What were you trying to do, and what happened instead?"></textarea></label><div class="turnstile-row"><div class="cf-turnstile" data-sitekey="__TURNSTILE_SITE_KEY__" data-action="bug-report" data-theme="light"></div><button id="send-bug-report" type="submit">Send bug report</button><span class="muted feedback-note">Please do not include passwords, calendar links, or private student information.</span></div><p class="feedback-unavailable" id="feedback-unavailable" hidden>Bug reporting is temporarily unavailable.</p></form></section></footer><p class="status" id="status"></p></main>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<script>
const $=s=>document.querySelector(s), esc=s=>String(s??""),feedbackConfigured=__FEEDBACK_CONFIGURED__; let state,googleDiscoveryFailures=new Map(),selectedStudentEmail='',lessonFilter='all';
async function api(url,opt={}){const r=await fetch(url,{...opt,headers:{"Content-Type":"application/json",...(opt.headers||{})}});if(!r.ok){let m="Request failed";try{m=(await r.json()).detail||m}catch{}throw Error(m)}return r.status===204?null:r.json()}
function field(form,n,v){const e=form.elements[n];if(e.type==="checkbox")e.checked=!!v;else e.value=v??""}
function packageRow(item={lesson_count:8,price_cents:0,is_active:true}){const row=document.createElement('div'),count=document.createElement('input'),discount=document.createElement('input'),price=document.createElement('input'),active=document.createElement('input'),remove=document.createElement('button');row.className='item package-row';count.type='number';count.min='2';count.max='100';count.value=item.lesson_count;count.setAttribute('aria-label','Lessons in package');discount.type='number';discount.min='0';discount.max='100';discount.step='.01';discount.setAttribute('aria-label','Package discount percent');price.type='number';price.min='0';price.step='.01';price.value=(item.price_cents/100).toFixed(2);price.setAttribute('aria-label','Package total price');active.type='checkbox';active.checked=item.is_active;active.setAttribute('aria-label','Package active');remove.type='button';remove.className='danger icon-button';remove.innerHTML=trashIcon;remove.setAttribute('aria-label','Remove package');const syncDiscount=()=>{const full=Number($('#pricing').elements.hourly_rate.value)*Number(count.value);discount.value=full>0?Math.max(0,100-Number(price.value)/full*100).toFixed(2):'0.00'};const syncPrice=()=>{const full=Number($('#pricing').elements.hourly_rate.value)*Number(count.value);price.value=Math.max(0,full*(1-Number(discount.value)/100)).toFixed(2)};count.oninput=syncDiscount;price.oninput=syncDiscount;discount.oninput=syncPrice;remove.onclick=()=>row.remove();row.append(count,discount,price,active,remove);row.dataset.labels='Lessons · Discount % · Total · Active';syncDiscount();return row}
function renderPackages(){const form=$('#pricing');field(form,'currency',state.profile.currency);field(form,'hourly_rate',state.profile.hourly_rate_cents/100);field(form,'cancellation_notice_hours',state.profile.cancellation_notice_hours);field(form,'late_cancellation_consumes_credit',state.profile.late_cancellation_consumes_credit);field(form,'cancellation_policy_text',state.profile.cancellation_policy_text);$('#packages').replaceChildren(...state.packages.map(packageRow))}
async function load(){state=await api('/api/scheduling/manage');const f=$('#profile');for(const [k,v] of Object.entries(state.profile))if(f.elements[k])field(f,k,v);field(f,'non_lesson_buffer_minutes',Math.max(state.profile.buffer_before_minutes,state.profile.buffer_after_minutes));$('#public-url').href=state.profile.public_url;$('#public-url').textContent=state.profile.public_url;renderPackages();render();renderBookingActions();renderStudents();renderMetrics()}
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
function lessonNode(x){const d=node(x.student_name,new Date(x.starts_at).toLocaleString()+` · ${x.student_email}`),badge=document.createElement('span');d.classList.add('lesson-item',x.category);badge.className='lesson-status';badge.textContent=x.category[0].toUpperCase()+x.category.slice(1);d.append(badge);if(x.category==='upcoming'){const cancel=document.createElement('button');cancel.className='danger';cancel.type='button';cancel.textContent='Cancel';cancel.onclick=async()=>{if(!confirm(`Cancel the lesson with ${x.student_name}?`))return;await api(`/api/scheduling/bookings/${x.id}/cancel`,{method:'POST',body:'{}'});await load();status('Lesson cancelled.')};d.append(cancel)}return d}
function renderBookingActions(){const rank={upcoming:0,completed:1,cancelled:2},items=state.bookings.filter(x=>lessonFilter==='all'||x.category===lessonFilter).sort((a,b)=>rank[a.category]-rank[b.category]||(a.category==='upcoming'?new Date(a.starts_at)-new Date(b.starts_at):new Date(b.starts_at)-new Date(a.starts_at))).map(lessonNode);$('#bookings').replaceChildren(...(items.length?items:[emptyNode(lessonFilter==='all'?'No lessons yet.':`No ${lessonFilter} lessons.`)]))}
function emptyNode(text){const d=document.createElement('div');d.className='empty';d.textContent=text;return d}
function studentNode(x){const card=document.createElement('div'),selectButton=document.createElement('button'),removeButton=document.createElement('button'),copy=document.createElement('span'),name=document.createElement('strong'),email=document.createElement('small'),balance=document.createElement('span'),remaining=Math.max(0,x.purchased-x.allocated);card.className='student-card'+(x.email===selectedStudentEmail?' active':'');selectButton.type='button';selectButton.className='student-select';name.textContent=x.name;email.textContent=`${x.email} · ${x.allocated} assigned`;balance.className='balance';balance.innerHTML=`<b>${remaining}</b> left`;copy.append(name,email);selectButton.append(copy,balance);selectButton.onclick=()=>{selectedStudentEmail=x.email;$('#payment').elements.student_email.value=x.email;renderStudents();$('#payment').scrollIntoView({behavior:'smooth',block:'nearest'})};removeButton.type='button';removeButton.className='danger icon-button student-remove';removeButton.setAttribute('aria-label',`Delete ${x.name}`);removeButton.title=`Delete ${x.name}`;removeButton.innerHTML=trashIcon;removeButton.onclick=async()=>{if(!confirm(`Remove ${x.name} from the student panel? Their lesson and payment history will be preserved.`))return;const result=await api(`/api/scheduling/students/${encodeURIComponent(x.email)}`,{method:'DELETE'});if(selectedStudentEmail===x.email){selectedStudentEmail='';$('#payment').elements.student_email.value=''}await load();status(result.hidden?'Student hidden; lesson and payment history preserved.':'Student deleted.')};card.append(selectButton,removeButton);return card}
function paymentNode(x){const amount=x.amount_cents===null?'Amount not recorded':money(x.amount_cents,x.currency),d=node(x.student_email,`${amount} · ${x.lessons_purchased} lessons · ${x.allocated} assigned · ${new Date(x.paid_at).toLocaleDateString()}`),remove=document.createElement('button');remove.type='button';remove.className='danger icon-button';remove.setAttribute('aria-label',`Delete payment for ${x.student_email}`);remove.title=`Delete payment for ${x.student_email}`;remove.innerHTML=trashIcon;remove.onclick=async()=>{if(!confirm(`Delete the registered payment for ${x.student_email}? Assigned lessons will become unpaid.`))return;await api(`/api/scheduling/student-payments/${x.id}`,{method:'DELETE'});await load();status('Registered payment deleted; lesson allocations removed.')};d.append(remove);return d}
function renderStudents(){const form=$('#payment'),input=form.elements.student_email,query=$('#student-search').value.trim().toLowerCase(),pricing=form.elements.pricing_option,current=pricing.value;pricing.replaceChildren(new Option('Custom payment','custom'),new Option(`Single lesson · ${money(state.profile.hourly_rate_cents)}`,'single'),...state.packages.filter(x=>x.is_active).map((x,index)=>new Option(`${x.lesson_count} lessons · ${money(x.price_cents)}`,String(index))));if([...pricing.options].some(x=>x.value===current))pricing.value=current;$('#payment-amount-label').textContent=`Amount paid (${state.profile.currency})`;if(!selectedStudentEmail&&input.value)selectedStudentEmail=input.value;if(selectedStudentEmail&&!input.value)input.value=selectedStudentEmail;$('#student-emails').replaceChildren(...state.students.map(x=>new Option(x.email)));const visible=state.students.filter(x=>!query||x.name.toLowerCase().includes(query)||x.email.toLowerCase().includes(query));$('#students').replaceChildren(...(visible.length?visible.map(studentNode):[emptyNode(query?'No matching students.':'Students appear after their first booking or payment.') ]));const student=state.students.find(x=>x.email===input.value);const bookings=student?student.bookings.filter(x=>!x.paid&&x.status!=='cancelled'):[];$('#payment-lessons').replaceChildren(...(bookings.length?bookings.map(x=>{const label=document.createElement('label'),box=document.createElement('input');label.className='row';box.type='checkbox';box.name='booking_id';box.value=x.id;box.style.width='auto';label.append(box,document.createTextNode(`${new Date(x.starts_at).toLocaleString()} · ${x.status}`));return label}):[emptyNode(student?'No unpaid lessons to assign.':'Select a student to assign lessons.')]));$('#registered-payments').replaceChildren(...(state.payments.length?state.payments.map(paymentNode):[emptyNode('No payments registered.')]))}
function money(cents,currency=state.profile.currency){return new Intl.NumberFormat(undefined,{style:'currency',currency}).format(cents/100)}
function renderMetrics(){$('#completed-count').textContent=state.metrics.completed_lessons;$('#monthly-count').textContent=state.metrics.total_lessons;$('#student-count').textContent=state.students.length;$('#earned-income').textContent=money(state.metrics.earned_cents);$('#projected-income').textContent=`${money(state.metrics.projected_cents)} projected from completed + scheduled lessons`}
function selectTab(name){document.querySelectorAll('.tab').forEach(button=>button.classList.toggle('active',button.dataset.tab===name));$('#welcome-panel').hidden=name!=='welcome';$('#setup-panel').hidden=name!=='setup';$('#lessons-panel').hidden=name!=='lessons';history.replaceState({},'',`${location.pathname}${location.search}#${name}`)}
const trashIcon='<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V4h6v3m-8 0 1 13h8l1-13M10 11v5m4-5v5"/></svg>',disableIcon=trashIcon,enableIcon='<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v8m-4-4h8"/></svg>';
function setWindowEnabled(d,enabled){d.classList.toggle('disabled',!enabled);d.querySelectorAll('select,input').forEach(control=>control.disabled=!enabled);const toggle=d.querySelector('.day-toggle'),day=d.querySelector('select').selectedOptions[0]?.text||'day';toggle.innerHTML=enabled?disableIcon:enableIcon;toggle.setAttribute('aria-label',enabled?`Disable ${day}`:`Enable ${day}`);toggle.title=enabled?`Disable ${day}`:`Enable ${day}`}
function windowNode(day=0,start='09:00',end='17:00',names=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'],enabled=true){const d=document.createElement('div');d.className='days';const s=document.createElement('select');s.setAttribute('aria-label','Day');names.forEach((n,i)=>s.add(new Option(n,i)));s.value=day;const a=document.createElement('input');a.type='time';a.setAttribute('aria-label','Available from');a.value=start;const b=document.createElement('input');b.type='time';b.setAttribute('aria-label','Available until');b.value=end;const toggle=document.createElement('button');toggle.type='button';toggle.className='day-toggle';toggle.onclick=()=>setWindowEnabled(d,d.classList.contains('disabled'));s.onchange=()=>setWindowEnabled(d,!d.classList.contains('disabled'));d.append(s,a,b,toggle);setWindowEnabled(d,enabled);return d}
function editLesson(x){const f=$('#lesson');for(const k of ['id','name','duration_minutes','location','description','is_active'])field(f,k,x[k]);f.scrollIntoView({behavior:'smooth'})}function status(s){$('#status').textContent=s;setTimeout(()=>$('#status').textContent='',4000)}
$('#profile').onsubmit=async e=>{e.preventDefault();const f=e.currentTarget,d=Object.fromEntries(new FormData(f)),buffer=Number(d.non_lesson_buffer_minutes);delete d.non_lesson_buffer_minutes;d.buffer_before_minutes=buffer;d.buffer_after_minutes=buffer;for(const n of ['minimum_notice_minutes','booking_window_days','slot_interval_minutes'])d[n]=Number(d[n]);d.is_active=f.elements.is_active.checked;await api('/api/scheduling/profile',{method:'PUT',body:JSON.stringify(d)});status('Settings saved.');await load()};
$('#add-package').onclick=()=>$('#packages').append(packageRow({lesson_count:8,price_cents:Math.round(Number($('#pricing').elements.hourly_rate.value)*800),is_active:true}));
$('#pricing').onsubmit=async e=>{e.preventDefault();const form=e.currentTarget,data=Object.fromEntries(new FormData(form)),packages=[...$('#packages').children].map(row=>({lesson_count:Number(row.children[0].value),price_cents:Math.round(Number(row.children[2].value)*100),is_active:row.children[3].checked}));const pricing={currency:String(data.currency).toUpperCase(),hourly_rate_cents:Math.round(Number(data.hourly_rate)*100),cancellation_notice_hours:Number(data.cancellation_notice_hours),late_cancellation_consumes_credit:form.elements.late_cancellation_consumes_credit.checked,cancellation_policy_text:data.cancellation_policy_text};await api('/api/scheduling/pricing',{method:'PUT',body:JSON.stringify(pricing)});await api('/api/scheduling/packages',{method:'PUT',body:JSON.stringify({packages})});status('Pricing and cancellation policy saved.');await load()};
$('#copy-public-link').onclick=async()=>{await navigator.clipboard.writeText(state.profile.public_url);status('Booking link copied.')};
$('#lesson').onsubmit=async e=>{e.preventDefault();const f=e.currentTarget,d=Object.fromEntries(new FormData(f)),id=d.id;d.duration_minutes=Number(d.duration_minutes);d.is_active=f.elements.is_active.checked;delete d.id;await api(id?`/api/scheduling/lesson-types/${id}`:'/api/scheduling/lesson-types',{method:id?'PUT':'POST',body:JSON.stringify(d)});f.reset();f.elements.duration_minutes.value=60;f.elements.is_active.checked=true;status('Lesson type saved.');await load()};
$('#add-window').onclick=()=>$('#availability').append(windowNode());$('#save-availability').onclick=async()=>{const rules=[...$('#availability').children].filter(d=>!d.classList.contains('disabled')).map(d=>({weekday:Number(d.children[0].value),starts_at:d.children[1].value,ends_at:d.children[2].value}));await api('/api/scheduling/availability',{method:'PUT',body:JSON.stringify({rules})});status('Availability saved.');await load()};
$('#discover').onclick=async()=>{status('Checking Google calendars…');const result=await api('/api/scheduling/calendars/discover',{method:'POST',body:'{}'});googleDiscoveryFailures=new Map((result.failures||[]).map(x=>[x.connection_id,x.message]));await load();status(result.failures?.length?`Loaded ${result.discovered} calendar${result.discovered===1?'':'s'}; ${result.failures.length} account${result.failures.length===1?' needs':'s need'} attention.`:`Found ${result.discovered} calendar${result.discovered===1?'':'s'}.`)};
$('#icloud').onsubmit=async e=>{e.preventDefault();const form=e.currentTarget,d=Object.fromEntries(new FormData(form));status('Connecting privately to iCloud…');const result=await api('/api/scheduling/icloud-connections',{method:'POST',body:JSON.stringify(d)});form.reset();await load();status(result.sync_warning||`Connected ${result.calendar_count} iCloud calendar${result.calendar_count===1?'':'s'}.`)};
$('#payment').elements.student_email.onchange=e=>{selectedStudentEmail=e.currentTarget.value;renderStudents()};
$('#payment').elements.student_email.oninput=renderStudents;
$('#payment').elements.pricing_option.onchange=e=>{const form=$('#payment'),value=e.currentTarget.value;if(value==='single'){form.elements.lessons_purchased.value=1;form.elements.amount_euros.value=(state.profile.hourly_rate_cents/100).toFixed(2)}else if(value!=='custom'){const item=state.packages.filter(x=>x.is_active)[Number(value)];if(item){form.elements.lessons_purchased.value=item.lesson_count;form.elements.amount_euros.value=(item.price_cents/100).toFixed(2)}}};
$('#student-search').oninput=renderStudents;
document.querySelectorAll('.lesson-filter').forEach(button=>button.onclick=()=>{lessonFilter=button.dataset.lessonFilter;document.querySelectorAll('.lesson-filter').forEach(item=>item.classList.toggle('active',item===button));renderBookingActions()});
document.querySelectorAll('.tab').forEach(button=>button.onclick=()=>selectTab(button.dataset.tab));
$('#start-setup').onclick=()=>selectTab('setup');
$('#payment').onsubmit=async e=>{e.preventDefault();const form=e.currentTarget,data=new FormData(form),euros=data.get('amount_euros'),payload={student_email:data.get('student_email'),lessons_purchased:Number(data.get('lessons_purchased')),amount_cents:euros===''?null:Math.round(Number(euros)*100),booking_ids:data.getAll('booking_id')};await api('/api/scheduling/student-payments',{method:'POST',body:JSON.stringify(payload)});form.reset();await load();status('Payment recorded and selected lessons marked as paid.')};
if(!feedbackConfigured){$('#send-bug-report').disabled=true;$('#feedback-unavailable').hidden=false;document.querySelector('.cf-turnstile').hidden=true}
$('#bug-report').onsubmit=async e=>{e.preventDefault();if(!feedbackConfigured)return;const form=e.currentTarget,data=new FormData(form),button=$('#send-bug-report'),token=String(data.get('cf-turnstile-response')||'');if(!token){status('Please complete the security check.');return}button.disabled=true;try{await api('/api/scheduling/bug-reports',{method:'POST',body:JSON.stringify({section:data.get('section'),message:data.get('message'),website:data.get('website'),turnstile_token:token})});form.reset();if(window.turnstile)turnstile.reset();status('Bug report sent. Thank you!')}catch(error){if(window.turnstile)turnstile.reset();status(error.message)}finally{button.disabled=false}};
document.addEventListener('unhandledrejection',e=>{e.preventDefault();status(e.reason?.message||'Something went wrong.')});load().then(()=>{const params=new URLSearchParams(location.search),result=params.get('calendar'),hash=location.hash.slice(1),known=['welcome','setup','lessons'],isNew=!state.lesson_types.length&&!state.calendars.length&&!state.bookings.length&&!state.payments.length;selectTab(result?'setup':known.includes(hash)?hash:isNew?'welcome':'lessons');if(result==='connected')status('Google Calendar connected and calendars loaded.');else if(result)status('Google connected, but calendars could not be loaded. Please reconnect and grant calendar access.');if(result)history.replaceState({},'',`${location.pathname}#setup`)}).catch(e=>status(e.message));
</script></body></html>"""

SELECTED_SUMMARY_HTML = r"""<section id="selected-summary" class="selected-summary hidden" aria-live="polite"><div><h3>Selected lessons</h3><p class="muted">Review your dates before confirming.</p></div><div id="selected-list" class="selected-list"></div></section>"""


PUBLIC_INFO_HTML = r"""<section class="booking-info"><div><h3>Pricing</h3><p class="muted" id="pricing-info">Loading pricing…</p></div><div><h3>Booking conditions</h3><p class="muted" id="booking-conditions">Loading booking conditions…</p></div></section>"""


ACCOUNT_CONTROL_CSS = (
    ".page-account{position:fixed;top:20px;right:24px;z-index:10}"
    ".signed-in{display:flex;align-items:center;gap:9px;padding:6px 7px 6px 11px;"
    "border:1px solid #dedff1;border-radius:13px;background:rgba(255,255,255,.94);"
    "box-shadow:0 8px 24px rgba(75,73,145,.1);backdrop-filter:blur(10px)}"
    ".signed-in span{max-width:220px;color:#716d87;font-size:11px;overflow:hidden;"
    "text-overflow:ellipsis}.signed-in strong{color:#494566}.signed-in form{display:flex}"
    ".signed-in button,.account-sign-in{min-height:32px;padding:6px 9px;border:1px solid "
    "#dedff1;border-radius:9px;background:#fff;color:#5549b8;font-size:11px;font-weight:750;"
    "cursor:pointer;text-decoration:none}"
    "@media(max-width:650px){body{padding-top:64px}.page-account{top:10px;right:10px}"
    ".signed-in span{max-width:180px}}"
)


STUDENT_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>My lessons</title><style>
:root{font-family:"Google Sans",Inter,system-ui,sans-serif;color:#28243f;background:#f8f7ff;--indigo:#5f72dc;--violet:#8b5bd6;--line:#dedff1;--muted:#716d87}*{box-sizing:border-box}body{margin:0;min-height:100vh;padding:42px 20px;background:radial-gradient(circle at 10% 5%,#e9efff 0,transparent 34%),radial-gradient(circle at 92% 8%,#f3eaff 0,transparent 31%),#f8f7ff}main{width:min(900px,100%);margin:auto}.header,.summary,.lesson{background:#fff;border:1px solid var(--line);border-radius:20px;box-shadow:0 14px 35px rgba(75,73,145,.08)}.header{padding:26px 30px;display:flex;justify-content:space-between;align-items:center;gap:18px}.header-actions{display:flex;align-items:center;justify-content:flex-end;gap:12px;flex-wrap:wrap}.account{display:flex;align-items:center;gap:10px;padding:7px 8px 7px 12px;border:1px solid var(--line);border-radius:14px;background:linear-gradient(135deg,#f7f9ff,#faf6ff)}.account-label{color:var(--muted);font-size:11px;line-height:1.25}.account-label strong{display:block;max-width:230px;color:#494566;font-size:12px;overflow:hidden;text-overflow:ellipsis}.account form{display:flex}.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;margin:18px 0;overflow:hidden}.metric{padding:22px;background:#fff}.metric strong{display:block;font-size:25px}.metric span,.muted{color:var(--muted);font-size:13px}.list{display:grid;gap:12px}.lesson{padding:18px 20px;display:flex;justify-content:space-between;align-items:center;gap:18px}.lesson.past{opacity:.5;background:#f8f8fc;box-shadow:none}.actions{display:flex;gap:8px;flex-wrap:wrap}a,button{font:inherit;font-weight:750;border-radius:11px;padding:9px 13px;text-decoration:none;cursor:pointer}a.primary,button.primary{border:0;color:#fff;background:linear-gradient(135deg,var(--indigo),var(--violet))}button.secondary,a.secondary{border:1px solid var(--line);color:#514d76;background:#fff}.account button{padding:7px 10px;font-size:12px}.badge{display:inline-block;margin-top:6px;padding:4px 8px;border-radius:999px;background:#efefff;color:#5549b8;font-size:12px;font-weight:700}.notice{color:#5549b8;margin:14px 0 0;font-size:13px}.error{color:#a6405d}dialog{width:min(440px,calc(100% - 32px));padding:0;border:1px solid var(--line);border-radius:22px;color:#28243f;background:#fff;box-shadow:0 24px 70px rgba(53,48,103,.28)}dialog::backdrop{background:rgba(40,36,63,.36);backdrop-filter:blur(3px)}.modal-body{padding:28px}.modal-icon{width:42px;height:42px;display:grid;place-items:center;margin-bottom:18px;border-radius:14px;background:linear-gradient(135deg,#e5ecff,#eee2ff);color:#6257bd;font-size:21px}.modal-body h2{margin:0 0 9px;font-size:22px}.modal-body p{margin:0;color:var(--muted);font-size:14px;line-height:1.55}.modal-note{margin-top:12px!important;padding:11px 12px;border-radius:11px;background:#f7f5ff;color:#5e5877!important;font-size:12px!important}.modal-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:24px}.modal-actions button{min-width:100px}@media(max-width:720px){.header,.lesson{align-items:flex-start;flex-direction:column}.header-actions,.account{width:100%}.header-actions{justify-content:flex-start}.account{justify-content:space-between}.summary{grid-template-columns:1fr}.actions{width:100%}.modal-actions{flex-direction-reverse}.modal-actions button{width:100%}}
</style></head><body>__STUDENT_ACCOUNT__<main><section class="header"><div><h1>My lessons</h1><p class="muted" id="identity"></p></div><div class="header-actions"><a class="primary" href="/book/__BOOKING_SLUG__">Book lessons</a></div></section><section class="summary"><div class="metric"><strong id="purchased">0</strong><span>Paid lessons</span></div><div class="metric"><strong id="allocated">0</strong><span>Assigned to lessons</span></div><div class="metric"><strong id="remaining">0</strong><span>Paid lessons remaining</span></div></section><div class="list" id="lessons"></div><p class="notice" id="notice" role="status"></p><p class="error" id="error"></p></main><dialog id="lesson-modal" aria-labelledby="modal-title"><div class="modal-body"><div class="modal-icon" aria-hidden="true">↻</div><h2 id="modal-title"></h2><p id="modal-message"></p><p class="modal-note" id="cancellation-note"></p><p class="error" id="modal-error" role="alert"></p><div class="modal-actions"><button class="secondary" id="modal-back" type="button">Keep lesson</button><button class="primary" id="modal-confirm" type="button"></button></div></div></dialog><script>
const slug='__BOOKING_SLUG__',$=s=>document.querySelector(s);async function api(u,o={}){const r=await fetch(u,{...o,headers:{'Content-Type':'application/json'}});if(!r.ok){let m='Request failed';try{m=(await r.json()).detail||m}catch{}throw Error(m)}return r.json()}function lessonNode(x){const n=document.createElement('article'),copy=document.createElement('div'),actions=document.createElement('div'),when=document.createElement('strong'),status=document.createElement('span');n.className=`lesson${x.is_past?' past':''}`;actions.className='actions';when.textContent=new Date(x.starts_at).toLocaleString([],{weekday:'long',day:'numeric',month:'long',year:'numeric',hour:'2-digit',minute:'2-digit'});status.className='badge';status.textContent=x.status==='cancelled'?'Cancelled':x.paid?'Paid':'Payment not recorded';copy.append(when,document.createElement('br'),status);if(x.can_cancel){const cancel=document.createElement('button'),move=document.createElement('button');cancel.className='secondary';cancel.textContent='Cancel';cancel.onclick=()=>openLessonModal(x.id,false);move.className='primary';move.textContent='Reschedule';move.onclick=()=>openLessonModal(x.id,true);actions.append(move,cancel)}else if(!x.is_past&&x.meeting_url&&x.status==='confirmed'){const meet=document.createElement('a');meet.className='primary';meet.href=x.meeting_url;meet.textContent='Join Meet';actions.append(meet)}n.append(copy,actions);return n}let pendingChange=null;function openLessonModal(id,reschedule){pendingChange={id,reschedule};$('#modal-title').textContent=reschedule?'Reschedule this lesson?':'Cancel this lesson?';$('#modal-message').textContent=reschedule?'This lesson will be cancelled first, then you can choose a replacement time.':'This removes the lesson from your schedule and both calendars.';$('#modal-confirm').textContent=reschedule?'Choose a new time':'Cancel lesson';$('#modal-error').textContent='';$('#lesson-modal').showModal()}function closeLessonModal(){pendingChange=null;$('#lesson-modal').close()}$('#modal-back').onclick=closeLessonModal;$('#lesson-modal').addEventListener('cancel',()=>{pendingChange=null});$('#modal-confirm').onclick=async()=>{if(!pendingChange)return;const{id,reschedule}=pendingChange;$('#modal-confirm').disabled=true;$('#modal-error').textContent='';try{const result=await api(`/api/public/scheduling/${slug}/my-lessons/${id}/cancel`,{method:'POST',body:'{}'});closeLessonModal();if(reschedule){location.href=`/book/${slug}`;return}$('#notice').textContent=result.late?'Lesson cancelled. Because it was within 12 hours, the credit was used.':'Lesson cancelled and the credit was restored.';await load()}catch(e){$('#modal-error').textContent=e.message}finally{$('#modal-confirm').disabled=false}};async function load(){const d=await api(`/api/public/scheduling/${slug}/my-lessons`);$('#identity').textContent=`Lessons with ${d.tutor_name}`;$('#account-email').textContent=d.student_email;$('#purchased').textContent=d.purchased;$('#allocated').textContent=d.allocated;$('#remaining').textContent=d.remaining;$('#lessons').replaceChildren(...d.lessons.slice().reverse().map(lessonNode))}load().catch(e=>$('#error').textContent=e.message);
</script></body></html>"""


STUDENT_HTML = (
    STUDENT_HTML.replace(
        ".lesson.past{opacity:.5;background:#f8f8fc;box-shadow:none}",
        ".lesson.past:not(.cancelled){opacity:.42;background:#f1f1f7;"
        "border-color:#e4e3ed;box-shadow:none;color:#767184}"
        ".lesson.cancelled{opacity:.62;background:linear-gradient(135deg,#faf9fd,#f5f3f9);"
        "border-style:dashed;box-shadow:none;color:#777187}"
        ".lesson.cancelled strong{text-decoration:line-through;text-decoration-thickness:1px}"
        ".lesson.cancelled .badge{background:#ece9f2;color:#827b99}",
    )
    .replace(
        "n.className=`lesson${x.is_past?' past':''}`;",
        "n.className=`lesson${isPast?' past':''}"
        "${x.status==='cancelled'?' cancelled':''}`;",
    )
    .replace(
        "function lessonNode(x){const n=",
        "function lessonNode(x){const isPast=x.is_past||"
        "new Date(x.ends_at)<=new Date(),n=",
    )
    .replace(
        "status.textContent=x.status==='cancelled'?'Cancelled':x.paid?'Paid':"
        "'Payment not recorded';",
        "status.textContent=x.status==='cancelled'?'Cancelled':isPast?'Past':"
        "x.paid?'Paid':'Payment not recorded';",
    )
    .replace("!x.is_past&&x.meeting_url", "!isPast&&x.meeting_url")
    .replace(
        "n.append(copy,actions);return n}let pendingChange=null;",
        "n.append(copy,actions);return n}"
        "function lessonOrder(items){return items.slice().sort((a,b)=>"
        "Number(a.status==='cancelled')-Number(b.status==='cancelled')||"
        "new Date(b.starts_at)-new Date(a.starts_at))}let pendingChange=null;",
    )
    .replace(
        "d.lessons.slice().reverse().map(lessonNode)",
        "lessonOrder(d.lessons).map(lessonNode)",
    )
    .replace(
        "Lesson cancelled. Because it was within 12 hours, the credit was used.",
        "Lesson cancelled and the credit was used under the cancellation policy.",
    )
    .replace(
        "async function load(){const d=await api(`/api/public/scheduling/${slug}/my-lessons`);",
        "async function load(){const d=await api(`/api/public/scheduling/${slug}/my-lessons`),"
        "deadline=d.cancellation_notice_hours===1?'1 hour':"
        "`${d.cancellation_notice_hours} hours`,late=d.late_cancellation_consumes_credit?"
        "'Changes after that use the lesson credit.':'Changes after that still restore the lesson credit.';",
    )
    .replace(
        "$('#remaining').textContent=d.remaining;$('#lessons').replaceChildren",
        "$('#remaining').textContent=d.remaining;$('#cancellation-note').textContent="
        "`Changes at least ${deadline} ahead restore the lesson credit. ${late}"
        "${d.cancellation_policy_text?' '+d.cancellation_policy_text:''}`;"
        "$('#lessons').replaceChildren",
    )
    .replace(
        "</style></head><body>",
        f"{ACCOUNT_CONTROL_CSS}</style></head><body>",
    )
)


PUBLIC_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Book a lesson</title><style>
.page-header{display:flex;justify-content:space-between;align-items:center;gap:18px}.page-header a{color:#5549b8;font-weight:750;text-decoration:none}a.primary{display:inline-flex;align-items:center;min-height:42px;background:linear-gradient(135deg,#5f72dc,#8b5bd6);color:#fff;padding:10px 18px;border-radius:11px;font-weight:750;text-decoration:none;box-shadow:0 7px 18px rgba(105,91,204,.24)}body .shell{width:min(1280px,100%)}.signin{position:relative;display:grid;grid-template-columns:minmax(0,1fr) 250px;align-items:center;min-height:360px;padding:48px 52px;background:linear-gradient(135deg,rgba(250,251,255,.98),rgba(250,246,255,.98))}.signin.hidden{display:none}.signin-copy{max-width:590px}.signin h2{font-size:clamp(27px,4vw,38px);margin:0 0 12px}.signin .muted{font-size:15px;line-height:1.6;margin:0 0 24px}.signin-sketch{position:relative;display:grid;place-items:center}.signin-sketch:before{content:"";position:absolute;width:235px;height:235px;border-radius:50%;background:linear-gradient(135deg,rgba(216,228,255,.72),rgba(238,216,255,.68))}.signin-sketch svg{position:relative;width:220px;opacity:.62}.selected-summary{margin-top:26px;padding:20px;border:1px solid #dedff1;border-radius:16px;background:linear-gradient(135deg,#f8faff,#fbf7ff)}.selected-summary>div:first-child{display:flex;align-items:baseline;justify-content:space-between;gap:16px}.selected-summary p{margin:0}.selected-list{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}.selected-chip{display:inline-flex;align-items:center;gap:9px;padding:8px 10px 8px 12px;border:1px solid #d5d7ef;border-radius:999px;background:#fff;color:#494566;font-size:12px;font-weight:700}.selected-chip button{width:22px;height:22px;min-height:0;padding:0;border:0;border-radius:50%;background:#eeecfb;color:#6257ac;box-shadow:none;cursor:pointer}.booking-info{display:grid;grid-template-columns:minmax(0,.8fr) minmax(0,1.4fr);gap:28px;margin-top:26px;padding:22px 0;border-top:1px solid #e4e3f2;border-bottom:1px solid #e4e3f2}.booking-info h3{margin-bottom:9px}.booking-info p{margin:0;line-height:1.65}@media(min-width:901px){body .booking-layout{grid-template-columns:230px minmax(0,1fr)}}@media(max-width:650px){.booking-info{grid-template-columns:1fr;gap:18px}.selected-summary>div:first-child{display:block}.selected-summary p{margin-top:5px}.signin{grid-template-columns:1fr;padding:36px 26px}.signin-sketch{margin-top:22px}.signin-sketch:before{width:175px;height:175px}.signin-sketch svg{width:165px}}
__ACCOUNT_CONTROL_CSS__
body .shell.signin-mode{width:min(780px,100%)}
:root{font-family:"Google Sans",Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;color:#28243f;background:#f8f7ff;--indigo:#5f72dc;--violet:#8b5bd6;--accent:#7065dc;--accent-dark:#5549b8;--accent-soft:#efefff;--surface:#fafaff;--line:#dedff1;--muted:#716d87}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:clamp(18px,4vw,48px);background:radial-gradient(circle at 10% 5%,#e9efff 0,transparent 34%),radial-gradient(circle at 92% 8%,#f3eaff 0,transparent 31%),linear-gradient(145deg,#f8f9ff,#faf7ff)}.shell{width:min(1100px,100%);background:rgba(255,255,255,.97);border:1px solid rgba(211,213,238,.95);border-radius:26px;box-shadow:0 22px 60px rgba(75,73,145,.13);overflow:hidden;backdrop-filter:blur(12px);transition:width .25s ease}.shell.confirmed{width:min(780px,100%)}.page-header{padding:28px 34px;border-bottom:1px solid #ebeaf6}.confirmed .page-header{padding:25px 32px;background:linear-gradient(120deg,rgba(239,243,255,.78),rgba(248,239,255,.7))}h1{font-size:clamp(25px,3vw,30px);line-height:1.2;letter-spacing:-.025em;margin:0}h2{font-size:17px;line-height:1.3;letter-spacing:-.015em;margin:0}h3{font-size:15px;line-height:1.35;margin:0}.booking-layout{display:grid;grid-template-columns:260px minmax(0,1fr);min-height:470px}.lesson-panel{padding:30px 26px;border-right:1px solid #ebeaf6;background:linear-gradient(180deg,#f9faff,#faf7ff)}.step-heading{display:flex;align-items:center;gap:10px;margin-bottom:18px}.step-number{width:28px;height:28px;display:grid;place-items:center;flex:0 0 auto;border-radius:10px;background:linear-gradient(135deg,#e8eeff,#f0e8ff);color:var(--accent-dark);font-size:13px;font-weight:800}.types{display:grid;gap:10px}.type-choice{width:100%;text-align:left;min-height:44px;border:1px solid #d9d9ee;background:#fff;color:#494566;border-radius:13px;padding:11px 13px;cursor:pointer;font-weight:700;font-size:13px;transition:.14s}.type-choice.active,.type-choice:hover{background:linear-gradient(135deg,#eef2ff,#f1eaff);border-color:#bfc3ec;color:var(--accent-dark);box-shadow:0 5px 14px rgba(95,114,220,.12)}.scheduler{padding:30px 32px}.calendar-layout{display:grid;grid-template-columns:minmax(330px,1fr) 220px;gap:28px}.calendar-panel{min-width:0}.calendar-toolbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}.calendar-nav{display:flex;gap:7px}.icon-button{width:36px;height:36px;border:1px solid var(--line);border-radius:50%;background:#fff;color:#514d76;display:grid;place-items:center;cursor:pointer;font-size:20px;line-height:1}.icon-button:hover:not(:disabled){background:linear-gradient(135deg,#eef2ff,#f2ebff);border-color:#c5c6ed}.icon-button:disabled{opacity:.3;cursor:default}.calendar{display:grid;grid-template-columns:repeat(7,minmax(36px,1fr));gap:6px}.weekday{text-align:center;color:#817d98;font-size:11px;font-weight:750;text-transform:uppercase;padding:6px 0}.calendar-blank{aspect-ratio:1}.day{width:100%;aspect-ratio:1;border:0;border-radius:50%;background:transparent;color:#9995a8;font-size:13px;font-weight:650;cursor:default}.day.available{background:linear-gradient(135deg,#edf2ff,#f2ebff);color:#4e4975;cursor:pointer}.day.available:hover{background:linear-gradient(135deg,#dfe7ff,#e8dcff);color:var(--accent-dark)}.day.selected{background:linear-gradient(135deg,var(--indigo),var(--violet));color:#fff;box-shadow:0 6px 16px rgba(105,91,204,.28)}.day.today:not(.selected){box-shadow:inset 0 0 0 1px #98a5e3}.time-panel{border-left:1px solid #ebeaf6;padding-left:24px;min-width:0}.time-panel .muted{font-size:12px;margin:6px 0 18px}.time-slots{display:grid;gap:8px;max-height:350px;overflow:auto;padding-right:4px}.time-placeholder{color:#817d98;font-size:13px;line-height:1.5;padding:12px 0}.time-choice{width:100%;min-height:40px;border:1px solid #d6d7ed;background:#fff;color:#494566;border-radius:11px;padding:9px 12px;cursor:pointer;font-size:13px;font-weight:750;transition:.14s}.time-choice:hover,.time-choice.active{background:linear-gradient(135deg,var(--indigo),var(--violet));border-color:transparent;color:#fff;box-shadow:0 6px 16px rgba(105,91,204,.24)}.details{margin-top:26px;border-top:1px solid #ebeaf6;padding-top:24px}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}.form-grid .full{grid-column:1/-1}label{display:grid;gap:7px;margin:12px 0;font-weight:650;font-size:13px}input,textarea,button{font:inherit}input,textarea{min-height:44px;padding:10px 12px;border:1px solid #d6d7ed;border-radius:11px;outline:none;background:#fff}textarea{min-height:82px;resize:vertical}input:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(112,101,220,.14)}button.primary{min-height:42px;border:0;background:linear-gradient(135deg,var(--indigo),var(--violet));color:#fff;padding:10px 18px;border-radius:11px;font-weight:750;cursor:pointer;box-shadow:0 7px 18px rgba(105,91,204,.24)}button.primary:hover{filter:brightness(.94)}.hidden{display:none}.error{color:#a6405d;margin:14px 0 0;font-size:13px}.success{position:relative;display:grid;grid-template-columns:minmax(0,1fr) 230px;align-items:center;min-height:330px;overflow:hidden;padding:46px 44px;background:linear-gradient(135deg,rgba(250,251,255,.98),rgba(250,246,255,.98))}.success.hidden{display:none}.success-copy{position:relative;z-index:1}.success-kicker{display:inline-flex;align-items:center;gap:8px;margin-bottom:18px;color:#584bb1;font-size:13px;font-weight:800;letter-spacing:.02em;text-transform:uppercase}.success-check{width:28px;height:28px;display:grid;place-items:center;border-radius:10px;background:linear-gradient(135deg,#dfe8ff,#eadcff);box-shadow:0 5px 14px rgba(105,91,204,.14)}.success-check svg{width:16px;height:16px}.success h2{font-size:clamp(28px,4vw,38px);line-height:1.08;letter-spacing:-.035em;margin:0 0 13px}.success-time{color:#4f4868;font-size:17px;font-weight:750;line-height:1.45;margin:0}.success-meta{color:var(--muted);font-size:14px;line-height:1.55;margin:10px 0 0}.success-note{display:flex;gap:10px;align-items:flex-start;margin:26px 0 0;padding-top:20px;border-top:1px solid #e5e2f1;color:#625b75;font-size:13px;line-height:1.5}.success-note svg{width:18px;flex:0 0 auto;color:#6d63ca;margin-top:1px}.success-sketch{position:relative;z-index:0;display:grid;place-items:center}.success-sketch:before{content:"";position:absolute;width:230px;height:230px;border-radius:50%;background:linear-gradient(135deg,rgba(216,228,255,.68),rgba(238,216,255,.65));filter:blur(1px)}.success-sketch svg{position:relative;width:230px;max-width:100%;opacity:.58;filter:drop-shadow(0 12px 18px rgba(91,82,157,.09))}.hp{position:absolute;left:-9999px}@media(max-width:900px){body{place-items:start center}.booking-layout{grid-template-columns:1fr}.lesson-panel{border-right:0;border-bottom:1px solid #ebeaf6;padding:22px 26px}.types{display:flex;flex-wrap:wrap}.type-choice{width:auto}.scheduler{padding:26px}.calendar-layout{grid-template-columns:minmax(300px,1fr) 200px;gap:22px}.time-panel{padding-left:20px}}@media(max-width:650px){body{padding:0}.shell{border-radius:0;border-left:0;border-right:0;min-height:100vh}.page-header{padding:22px}.confirmed .page-header{padding:22px}.scheduler{padding:22px}.calendar-layout{grid-template-columns:1fr}.time-panel{border-left:0;border-top:1px solid #ebeaf6;padding:20px 0 0}.time-slots{grid-template-columns:repeat(2,1fr);max-height:none}.form-grid{grid-template-columns:1fr}.form-grid .full{grid-column:auto}.success{grid-template-columns:1fr;min-height:0;padding:38px 26px 30px}.success-sketch{margin:18px auto -18px}.success-sketch:before{width:180px;height:180px}.success-sketch svg{width:180px}.success-note{margin-top:20px}}
.signin-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.signin-actions button.secondary{min-height:42px;padding:10px 16px;border:1px solid var(--line);border-radius:11px;background:#fff;color:var(--accent-dark);font-weight:750;cursor:pointer}.guest-benefits{margin:10px 0 0!important;font-size:12px!important}.guest-details{padding:14px 16px;border:1px solid var(--line);border-radius:14px;background:linear-gradient(135deg,#f8faff,#fbf7ff)}.calendar{grid-template-columns:repeat(7,minmax(36px,52px));justify-content:space-between;gap:8px 6px}@media(max-width:420px){.calendar{grid-template-columns:repeat(7,minmax(30px,1fr));justify-content:normal;gap:4px}}
</style></head><body><main class="shell"><header class="page-header"><h1 id="title">Book a lesson</h1><a id="my-lessons" href="/book/__BOOKING_SLUG__/lessons">My lessons</a></header><section id="login" class="signin hidden"><div class="signin-copy"><div class="success-kicker">Welcome</div><h2>Choose how to book.</h2><p class="muted">Sign in with Google to book multiple lessons, manage cancellations and rescheduling, track lesson credits, and reuse your permanent Meet link.</p><div class="signin-actions"><a class="primary" href="/auth/google/start?next=book:__BOOKING_SLUG__">Continue with Google</a><button class="secondary" id="guest-booking" type="button">Book one session without signing in</button></div></div><div class="signin-sketch" aria-hidden="true"><svg viewBox="0 0 240 220" fill="none"><defs><linearGradient id="signin-line" x1="36" y1="24" x2="206" y2="202" gradientUnits="userSpaceOnUse"><stop stop-color="#5976df"/><stop offset="1" stop-color="#9860d7"/></linearGradient></defs><g stroke="url(#signin-line)" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><path d="M54 54c0-9 7-16 16-16h103c9 0 16 7 16 16v112c0 9-7 16-16 16H70c-9 0-16-7-16-16V54Z" fill="#fff" fill-opacity=".45"/><path d="M54 72h135M84 29v19m75-19v19M82 102h24m14 0h25m14 0h12M82 126h24m14 0h25m14 0h12M82 150h24m14 0h25"/><circle cx="175" cy="159" r="24" fill="#fff" fill-opacity=".5"/><path d="M166 159h18m-9-9v18"/></g></svg></div></section><div id="app" class="booking-layout"><aside class="lesson-panel"><div class="step-heading"><span class="step-number">1</span><h2>Choose a lesson</h2></div><div class="types" id="types"></div></aside><section class="scheduler"><div class="step-heading"><span class="step-number">2</span><h2 id="selection-heading">Choose up to 10 times</h2></div><div class="calendar-layout"><div class="calendar-panel"><div class="calendar-toolbar"><h2 id="month-label">Select a lesson</h2><div class="calendar-nav"><button class="icon-button" id="previous-month" type="button" aria-label="Previous month">‹</button><button class="icon-button" id="next-month" type="button" aria-label="Next month">›</button></div></div><div class="calendar" id="calendar"></div></div><aside class="time-panel"><h3 id="selected-date">Select a date</h3><p class="muted" id="tz"></p><p class="muted" id="selection-count">0 of 10 selected</p><div class="time-slots" id="slots"><p class="time-placeholder">Choose a lesson to view available dates.</p></div></aside></div><form id="booking" class="details hidden"><h2>Finish booking</h2><div id="guest-details" class="guest-details hidden"><div class="form-grid"><label>Your name<input name="student_name" maxlength="255" autocomplete="name"></label><label>Email<input name="student_email" type="email" maxlength="320" autocomplete="email"></label></div><p class="muted guest-benefits">A one-time Google Meet link and calendar invitation will be sent to this email.</p></div><div class="form-grid"><label class="full">Anything I should know?<textarea name="notes" maxlength="2000"></textarea></label></div><label class="hp">Website<input name="website" tabindex="-1" autocomplete="off"></label><button class="primary" id="confirm-booking">Confirm selected lessons</button></form><p id="error" class="error"></p></section></div><section id="success" class="success hidden" aria-live="polite"><div class="success-copy"><div class="success-kicker"><span class="success-check"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 12 4 4 8-9"/></svg></span><span id="success-kicker-label">Lessons confirmed</span></div><h2>You’re all set.</h2><p id="success-time" class="success-time"></p><p id="success-meta" class="success-meta"></p><p class="success-note"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="3"/><path d="M8 3v4m8-4v4M3 10h18"/></svg><span id="success-note-text">Your permanent Google Meet link is included in every calendar invitation.</span></p></div><div class="success-sketch" aria-hidden="true"><svg viewBox="0 0 260 250" fill="none"><defs><linearGradient id="sketch" x1="42" y1="30" x2="218" y2="223" gradientUnits="userSpaceOnUse"><stop stop-color="#5976df"/><stop offset="1" stop-color="#9860d7"/></linearGradient></defs><g stroke="url(#sketch)" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"><path d="M59 58c0-10 8-18 18-18h112c10 0 18 8 18 18v123c0 10-8 18-18 18H77c-10 0-18-8-18-18V58Z" fill="#fff" fill-opacity=".52"/><path d="M59 76h148M91 31v20m84-20v20"/><path d="M96 116h74M96 138h47" opacity=".75"/><circle cx="173" cy="158" r="24" fill="#fff" fill-opacity=".55"/><path d="m161 158 8 8 16-18"/></g></svg></div></section></main>
<script>
const slug='__BOOKING_SLUG__',signedIn=__STUDENT_SIGNED_IN__,$=s=>document.querySelector(s),timezone=Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';
let profile,type,calendarMonth,selectedDayKey,guestMode=false;let selectedStarts=[],slotsByDay=new Map(),loadedMonths=new Set();
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
function chooseSlot(date){const limit=guestMode?1:10,key=date.toISOString(),index=selectedStarts.findIndex(x=>x.toISOString()===key);if(index>=0)selectedStarts.splice(index,1);else if(selectedStarts.length<limit)selectedStarts.push(date);else{$('#error').textContent=guestMode?'Guests can book one session at a time. Sign in with Google to book more.':'You can book up to 10 lessons at once.';return}$('#selection-count').textContent=`${selectedStarts.length} of ${limit} selected`;$('#booking').classList.toggle('hidden',!selectedStarts.length);renderTimes();renderSelection()}
async function chooseType(item,button){const limit=guestMode?1:10;type=item;selectedStarts=[];selectedDayKey=null;slotsByDay=new Map();loadedMonths=new Set();calendarMonth=new Date(new Date().getFullYear(),new Date().getMonth(),1);[...$('#types').children].forEach(n=>n.classList.remove('active'));button.classList.add('active');$('#booking').classList.add('hidden');$('#selection-count').textContent=`0 of ${limit} selected`;renderSelection();$('#error').textContent='';await loadMonth()}
async function changeMonth(offset){const next=new Date(calendarMonth.getFullYear(),calendarMonth.getMonth()+offset,1);if(!canShowMonth(next))return;calendarMonth=next;selectedDayKey=null;await loadMonth()}
async function initializeBooking(){ $('#tz').textContent=`Times shown in ${timezone.replaceAll('_',' ')}`;const buttons=profile.lesson_types.map(typeButton);$('#types').replaceChildren(...buttons);if(buttons.length)await chooseType(profile.lesson_types[0],buttons[0]);else $('#error').textContent='No lesson types are currently available.'}
async function startGuestBooking(){guestMode=true;$('#login').classList.add('hidden');$('#app').classList.remove('hidden');$('.shell').classList.remove('signin-mode');$('#selection-heading').textContent='Choose one time';$('#selection-count').textContent='0 of 1 selected';$('#guest-details').classList.remove('hidden');$('#booking').elements.student_name.required=true;$('#booking').elements.student_email.required=true;$('#confirm-booking').textContent='Confirm session';await initializeBooking()}
function renderPublicTerms(){const format=cents=>new Intl.NumberFormat(undefined,{style:'currency',currency:profile.currency}).format(cents/100),pricing=[`Hourly fee — ${format(profile.hourly_rate_cents)}`,...profile.packages.map(item=>{const each=item.lesson_count?item.price_cents/item.lesson_count:0;return `${item.lesson_count} lessons — ${format(item.price_cents)} (${format(each)} each)`})],pricingNode=$('#pricing-info');pricingNode.replaceChildren(...pricing.flatMap((line,index)=>index?[document.createElement('br'),document.createTextNode(line)]:[document.createTextNode(line)]));const deadline=profile.cancellation_notice_hours===1?'1 hour':`${profile.cancellation_notice_hours} hours`,late=profile.late_cancellation_consumes_credit?'Later changes remain available but use the lesson credit.':'Later changes remain available and restore the lesson credit.',parts=[`Book one session as a guest, or sign in with Google to book up to 10 lessons at once. Packages are valid for five weeks from the first booked lesson. Cancellations and rescheduling at least ${deadline} ahead restore the lesson credit. ${late}`];if(profile.cancellation_policy_text)parts.push(profile.cancellation_policy_text);$('#booking-conditions').textContent=parts.join(' ')}
async function load(){profile=await api(`/api/public/scheduling/${slug}`);$('#title').textContent=`Book a lesson with ${profile.display_name}`;renderPublicTerms();if(!signedIn){$('#app').classList.add('hidden');$('#my-lessons').classList.add('hidden');$('.shell').classList.add('signin-mode');$('#login').classList.remove('hidden');return}await initializeBooking()}
$('#guest-booking').onclick=()=>startGuestBooking().catch(e=>$('#error').textContent=e.message);$('#previous-month').onclick=()=>changeMonth(-1);$('#next-month').onclick=()=>changeMonth(1);$('#booking').onsubmit=async e=>{e.preventDefault();$('#error').textContent='';const d=Object.fromEntries(new FormData(e.currentTarget));Object.assign(d,{lesson_type_id:type.id,starts_at:selectedStarts.map(x=>x.toISOString()),student_timezone:timezone});try{const result=await api(`/api/public/scheduling/${slug}/book`,{method:'POST',body:JSON.stringify(d)}),first=new Date(result.bookings[0].starts_at);$('#app').classList.add('hidden');$('.shell').classList.add('confirmed');$('#title').textContent=result.count===1?'Your lesson is booked':'Your lessons are booked';$('#success-kicker-label').textContent=guestMode?'Session confirmed':'Lessons confirmed';$('#success-time').textContent=result.count===1?first.toLocaleString():`${result.count} lessons booked`;$('#success-meta').textContent=`${type.name} · ${type.duration_minutes} minutes · ${timezone.replaceAll('_',' ')}`;if(guestMode)$('#success-note-text').textContent='Your one-time Google Meet link is included in the calendar invitation sent to your email.';$('#success').classList.remove('hidden')}catch(err){$('#error').textContent=err.message}};load().catch(e=>$('#error').textContent=e.message);
</script></body></html>"""
