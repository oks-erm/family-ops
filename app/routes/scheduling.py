import html
import re
from datetime import UTC, date, datetime, time
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db.models import (
    CalendarEventCache,
    ICalFeed,
    LessonBooking,
    LessonType,
    SchedulingCalendar,
)
from app.db.repositories.calendar import CalendarRepository
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.scheduling import SchedulingRepository
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory
from app.services.calendar_service import (
    CalendarEventMatchError,
    CalendarNotConnectedError,
    CalendarService,
    CalendarSyncError,
    CalendarWritePermissionError,
)
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


class BookingRequest(BaseModel):
    lesson_type_id: UUID
    starts_at: datetime
    student_name: str = Field(min_length=1, max_length=255)
    student_email: str = Field(min_length=3, max_length=320)
    student_timezone: str = Field(min_length=1, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)
    website: str | None = Field(default=None, max_length=200)  # Honeypot.


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


@router.get("/schedule/manage", response_class=HTMLResponse)
async def scheduling_management_page(request: Request):
    if not request.session.get("google_email"):
        return RedirectResponse("/auth/google/start?next=scheduling", status_code=303)
    dashboard_url = f"{get_settings().public_base_url.rstrip('/')}/dashboard"
    return HTMLResponse(
        MANAGEMENT_HTML.replace("__DASHBOARD_URL__", html.escape(dashboard_url, quote=True))
    )


@router.get("/api/scheduling/manage")
async def scheduling_management_data(request: Request) -> dict[str, object]:
    async with async_session_factory() as session:
        _, household, profile = await _management_profile(request, session)
        repository = SchedulingRepository(session)
        connections = {
            item.id: item
            for item in await CalendarRepository(session).list_google_connections(
                household_id=household.id
            )
        }
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
                    "access_role": item.access_role,
                    "include_in_conflicts": item.include_in_conflicts,
                    "can_write": item.can_write,
                }
                for item in await repository.list_calendars(profile_id=profile.id)
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
        if booking.status == "cancelled":
            return {"cancelled": True}
        if booking.external_event_id:
            try:
                await CalendarService(session).delete_google_event_by_id(
                    household_id=profile.household_id,
                    calendar_id=booking.external_calendar_id,
                    event_id=booking.external_event_id,
                )
            except (
                CalendarNotConnectedError,
                CalendarWritePermissionError,
                httpx.HTTPError,
            ) as exc:
                raise HTTPException(
                    status_code=503,
                    detail="The calendar event could not be removed, so the lesson was not cancelled.",
                ) from exc
        booking.status = "cancelled"
        await session.commit()
        return {"cancelled": True}


@router.post("/api/scheduling/calendars/discover")
async def discover_calendars(request: Request) -> dict[str, int]:
    _require_same_origin(request)
    async with async_session_factory() as session:
        _, _, profile = await _management_profile(request, session)
        try:
            count = await SchedulingService(session).discover_google_calendars(profile=profile)
            await CalendarService(session).sync_google_connections(
                household_id=profile.household_id
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                raise HTTPException(
                    status_code=400,
                    detail="Reconnect Google Calendar to grant access to the calendar list.",
                ) from exc
            raise HTTPException(
                status_code=502, detail="Google Calendar discovery failed."
            ) from exc
        return {"discovered": count}


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
            try:
                await CalendarService(session).sync_google_connections(
                    household_id=profile.household_id
                )
            except (CalendarSyncError, httpx.HTTPError):
                return {
                    "saved": True,
                    "sync_warning": "Calendar selected; its first sync will retry within five minutes.",
                }
        return {"saved": True}


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
async def public_booking_page(slug: str) -> HTMLResponse:
    safe_slug = html.escape(slug, quote=True)
    return HTMLResponse(PUBLIC_HTML.replace("__BOOKING_SLUG__", safe_slug))


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
async def public_book(slug: str, payload: BookingRequest) -> dict[str, object]:
    if payload.website:
        raise HTTPException(status_code=400, detail="Invalid booking request.")
    if not _email_is_valid(payload.student_email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
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
            booking = await SchedulingService(session).book(
                profile=profile,
                lesson_type=lesson_type,
                starts_at=payload.starts_at,
                student_name=payload.student_name,
                student_email=payload.student_email,
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
            "id": str(booking.id),
            "status": booking.status,
            "starts_at": booking.starts_at.isoformat(),
            "ends_at": booking.ends_at.isoformat(),
        }


MANAGEMENT_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lesson scheduling</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#17221d;background:#f2f6f1}*{box-sizing:border-box}body{margin:0}
main{max-width:1080px;margin:auto;padding:28px 18px 80px}header{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-bottom:24px}
h1{font-size:clamp(28px,5vw,46px);margin:0}.muted{color:#627067}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:16px}
.card{background:#fff;border:1px solid #dce6de;border-radius:18px;padding:20px;box-shadow:0 10px 30px #183c2510}.wide{grid-column:1/-1}
label{display:grid;gap:6px;font-size:13px;font-weight:650;margin:11px 0}input,select,textarea,button{font:inherit}input,select,textarea{width:100%;padding:10px 12px;border:1px solid #cad7cd;border-radius:10px;background:white}button,.button{border:0;border-radius:10px;padding:10px 14px;background:#176b45;color:white;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block}.secondary{background:#e8f1eb;color:#24543b}.danger{background:#fff0ee;color:#9b2e23}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.list{display:grid;gap:9px;margin-top:12px}.item{display:flex;gap:12px;align-items:center;justify-content:space-between;border:1px solid #e3e9e4;border-radius:12px;padding:11px}.item p{margin:2px 0}.days{display:grid;grid-template-columns:110px 1fr 1fr auto;gap:8px;align-items:center}.status{min-height:24px;color:#176b45}.hidden{position:absolute;left:-9999px}
</style></head><body><main><header><div><p class="muted">Family Copilot</p><h1>Lesson scheduling</h1></div><a class="button secondary" href="__DASHBOARD_URL__">Dashboard</a></header>
<div class="grid"><section class="card"><h2>Booking page</h2><form id="profile"><label>Your public name<input name="display_name" required></label><label>Booking link<input name="slug" required></label><label>Timezone<input name="timezone" required></label><div class="row"><label>Minimum notice (minutes)<input name="minimum_notice_minutes" type="number" min="0"></label><label>Booking window (days)<input name="booking_window_days" type="number" min="1"></label></div><div class="row"><label>Buffer before<input name="buffer_before_minutes" type="number" min="0"></label><label>Buffer after<input name="buffer_after_minutes" type="number" min="0"></label><label>Slot interval<input name="slot_interval_minutes" type="number" min="5"></label></div><label>Calendar for new lessons<select name="booking_calendar_id"><option value="">Primary Google calendar</option></select></label><label class="row"><input name="is_active" type="checkbox" style="width:auto">Accept bookings</label><button>Save settings</button></form><p id="public-link"></p></section>
<section class="card"><h2>Lesson types</h2><form id="lesson"><input name="id" type="hidden"><label>Name<input name="name" required placeholder="English lesson"></label><label>Length (minutes)<input name="duration_minutes" type="number" min="15" value="60" required></label><label>Location or call link<input name="location"></label><label>Description<textarea name="description"></textarea></label><label class="row"><input name="is_active" type="checkbox" style="width:auto" checked>Active</label><button>Save lesson type</button></form><div class="list" id="lessons"></div></section>
<section class="card wide"><h2>Weekly availability</h2><p class="muted">Add one or more windows per day. Times use your timezone.</p><div class="list" id="availability"></div><div class="row"><button id="add-window" class="secondary" type="button">Add window</button><button id="save-availability" type="button">Save availability</button></div></section>
<section class="card"><h2>Google calendars</h2><p class="muted">Selected calendars block lesson availability. Reconnect if calendar discovery asks for permission.</p><div class="row"><a class="button secondary" href="/calendar/google/start?next=scheduling">Connect another account</a><button id="discover" type="button">Refresh calendars</button></div><div class="list" id="calendars"></div></section>
<section class="card"><h2>iCloud / Exchange / iCal</h2><p class="muted">Add a private subscription URL. These calendars are used for conflicts and remain read-only.</p><form id="ical"><label>Name<input name="name" required></label><label>Private iCal URL<input name="url" type="url" required></label><button>Add calendar</button></form><div class="list" id="feeds"></div></section>
<section class="card wide"><h2>Upcoming lessons</h2><div class="list" id="bookings"></div></section></div><p class="status" id="status"></p></main>
<script>
const $=s=>document.querySelector(s), esc=s=>String(s??""); let state;
async function api(url,opt={}){const r=await fetch(url,{...opt,headers:{"Content-Type":"application/json",...(opt.headers||{})}});if(!r.ok){let m="Request failed";try{m=(await r.json()).detail||m}catch{}throw Error(m)}return r.status===204?null:r.json()}
function field(form,n,v){const e=form.elements[n];if(e.type==="checkbox")e.checked=!!v;else e.value=v??""}
async function load(){state=await api('/api/scheduling/manage');const f=$('#profile');for(const [k,v] of Object.entries(state.profile))if(f.elements[k])field(f,k,v);$('#public-link').innerHTML=`Public page: <a href="${state.profile.public_url}" target="_blank" rel="noopener">${state.profile.public_url}</a>`;render();renderFeedActions();renderBookingActions()}
function render(){const names=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];$('#lessons').replaceChildren(...state.lesson_types.map(x=>node(`${x.name} · ${x.duration_minutes} min`,x.is_active?'Active':'Inactive',()=>editLesson(x))));const calSelect=$('#profile').elements.booking_calendar_id;calSelect.replaceChildren(new Option('Primary Google calendar',''),...state.calendars.filter(x=>x.can_write).map(x=>new Option(`${x.name}${x.account_email?' · '+x.account_email:''}`,x.external_calendar_id)));calSelect.value=state.profile.booking_calendar_id||'';$('#calendars').replaceChildren(...state.calendars.map(x=>{const d=document.createElement('label');d.className='item';const t=document.createElement('span');t.textContent=`${x.name}${x.account_email?' · '+x.account_email:''} (${x.access_role||'read'})`;const c=document.createElement('input');c.type='checkbox';c.style.width='auto';c.checked=x.include_in_conflicts;c.onchange=async()=>{await api(`/api/scheduling/calendars/${x.id}`,{method:'PUT',body:JSON.stringify({include_in_conflicts:c.checked})});status('Calendar selection saved.')};d.append(t,c);return d}));$('#feeds').replaceChildren(...state.ical_feeds.map(x=>node(x.name,'Read-only',async()=>{if(!confirm(`Remove ${x.name}?`))return;await api(`/api/scheduling/ical-feeds/${x.id}`,{method:'DELETE'});await load()})));$('#bookings').replaceChildren(...(state.bookings.length?state.bookings.map(x=>node(x.student_name,new Date(x.starts_at).toLocaleString()+` · ${x.student_email}`)): [node('No upcoming lessons','')]));$('#availability').replaceChildren(...state.availability.map(x=>windowNode(x.weekday,x.starts_at,x.ends_at,names)))}
function node(a,b,click,label='Edit'){const d=document.createElement('div');d.className='item';const s=document.createElement('span'),p=document.createElement('strong'),m=document.createElement('p');p.textContent=a;m.textContent=b;m.className='muted';s.append(p,m);d.append(s);if(click){const bt=document.createElement('button');bt.className=label==='Edit'?'secondary':'danger';bt.textContent=label;bt.onclick=click;d.append(bt)}return d}
function renderBookingActions(){if(!state.bookings.length)return;$('#bookings').replaceChildren(...state.bookings.map(x=>node(x.student_name,new Date(x.starts_at).toLocaleString()+` · ${x.student_email}`,async()=>{if(!confirm(`Cancel the lesson with ${x.student_name}?`))return;await api(`/api/scheduling/bookings/${x.id}/cancel`,{method:'POST',body:'{}'});await load();status('Lesson cancelled.')},'Cancel')))}
function renderFeedActions(){$('#feeds').replaceChildren(...state.ical_feeds.map(x=>node(x.name,'Read-only',async()=>{if(!confirm(`Remove ${x.name}?`))return;await api(`/api/scheduling/ical-feeds/${x.id}`,{method:'DELETE'});await load()},'Remove')))}
function windowNode(day=0,start='09:00',end='17:00',names=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']){const d=document.createElement('div');d.className='days';const s=document.createElement('select');names.forEach((n,i)=>s.add(new Option(n,i)));s.value=day;const a=document.createElement('input');a.type='time';a.value=start;const b=document.createElement('input');b.type='time';b.value=end;const remove=document.createElement('button');remove.type='button';remove.className='danger';remove.textContent='Remove';remove.onclick=()=>d.remove();d.append(s,a,b,remove);return d}
function editLesson(x){const f=$('#lesson');for(const k of ['id','name','duration_minutes','location','description','is_active'])field(f,k,x[k]);f.scrollIntoView({behavior:'smooth'})}function status(s){$('#status').textContent=s;setTimeout(()=>$('#status').textContent='',4000)}
$('#profile').onsubmit=async e=>{e.preventDefault();const f=e.currentTarget,d=Object.fromEntries(new FormData(f));for(const n of ['minimum_notice_minutes','booking_window_days','buffer_before_minutes','buffer_after_minutes','slot_interval_minutes'])d[n]=Number(d[n]);d.is_active=f.elements.is_active.checked;await api('/api/scheduling/profile',{method:'PUT',body:JSON.stringify(d)});status('Settings saved.');await load()};
$('#lesson').onsubmit=async e=>{e.preventDefault();const f=e.currentTarget,d=Object.fromEntries(new FormData(f)),id=d.id;d.duration_minutes=Number(d.duration_minutes);d.is_active=f.elements.is_active.checked;delete d.id;await api(id?`/api/scheduling/lesson-types/${id}`:'/api/scheduling/lesson-types',{method:id?'PUT':'POST',body:JSON.stringify(d)});f.reset();f.elements.duration_minutes.value=60;f.elements.is_active.checked=true;status('Lesson type saved.');await load()};
$('#add-window').onclick=()=>$('#availability').append(windowNode());$('#save-availability').onclick=async()=>{const rules=[...$('#availability').children].map(d=>({weekday:Number(d.children[0].value),starts_at:d.children[1].value,ends_at:d.children[2].value}));await api('/api/scheduling/availability',{method:'PUT',body:JSON.stringify({rules})});status('Availability saved.');await load()};
$('#discover').onclick=async()=>{status('Checking Google calendars…');await api('/api/scheduling/calendars/discover',{method:'POST',body:'{}'});await load();status('Calendars refreshed.')};$('#ical').onsubmit=async e=>{e.preventDefault();const d=Object.fromEntries(new FormData(e.currentTarget));await api('/api/scheduling/ical-feeds',{method:'POST',body:JSON.stringify(d)});e.currentTarget.reset();await load();status('Calendar added.')};
document.addEventListener('unhandledrejection',e=>{e.preventDefault();status(e.reason?.message||'Something went wrong.')});load().catch(e=>status(e.message));
</script></body></html>"""


PUBLIC_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Book a lesson</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#17221d;background:#f3f7f2}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:22px}.shell{width:min(820px,100%);background:white;border:1px solid #dce6de;border-radius:24px;padding:clamp(20px,5vw,44px);box-shadow:0 22px 60px #173d2520}h1{font-size:clamp(30px,7vw,52px);margin:.15em 0}.muted{color:#647269}.steps{display:grid;gap:18px}.types,.slots{display:flex;gap:9px;flex-wrap:wrap}.choice{border:1px solid #cbd9ce;background:#fff;color:#24402f;border-radius:12px;padding:11px 14px;cursor:pointer}.choice.active,.choice:hover{background:#176b45;color:white;border-color:#176b45}label{display:grid;gap:6px;margin:10px 0;font-weight:650;font-size:14px}input,textarea,button{font:inherit}input,textarea{padding:11px 12px;border:1px solid #cbd9ce;border-radius:10px}button.primary{border:0;background:#176b45;color:#fff;padding:12px 18px;border-radius:10px;font-weight:750;cursor:pointer}.hidden{display:none}.error{color:#a33228}.success{padding:20px;background:#eaf6ed;border-radius:14px}.hp{position:absolute;left:-9999px}
</style></head><body><main class="shell"><p class="muted">Lesson scheduling</p><h1 id="title">Book a lesson</h1><div id="app" class="steps"><section><h2>Choose a lesson</h2><div class="types" id="types"></div></section><section><h2>Choose a time</h2><p class="muted" id="tz"></p><div class="slots" id="slots"></div></section><form id="booking" class="hidden"><h2>Your details</h2><label>Name<input name="student_name" required maxlength="255"></label><label>Email<input name="student_email" type="email" required maxlength="320"></label><label>Anything I should know?<textarea name="notes" maxlength="2000"></textarea></label><label class="hp">Website<input name="website" tabindex="-1" autocomplete="off"></label><button class="primary">Confirm lesson</button></form><p id="error" class="error"></p></div><div id="success" class="success hidden"></div></main>
<script>
const slug='__BOOKING_SLUG__',$=s=>document.querySelector(s);let profile,type,start;const timezone=Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';async function api(u,o={}){const r=await fetch(u,{...o,headers:{'Content-Type':'application/json'}});if(!r.ok){let m='Request failed';try{m=(await r.json()).detail||m}catch{}throw Error(m)}return r.json()}function button(text,fn){const b=document.createElement('button');b.type='button';b.className='choice';b.textContent=text;b.onclick=()=>fn(b);return b}async function load(){profile=await api(`/api/public/scheduling/${slug}`);$('#title').textContent=`Book a lesson with ${profile.display_name}`;$('#tz').textContent=`Times shown in ${timezone.replaceAll('_',' ')}`;$('#types').replaceChildren(...profile.lesson_types.map(x=>button(`${x.name} · ${x.duration_minutes} min`,b=>chooseType(x,b))))}async function chooseType(x,b){type=x;[...$('#types').children].forEach(n=>n.classList.remove('active'));b.classList.add('active');const today=new Date(),end=new Date(today);end.setDate(end.getDate()+13);const iso=d=>d.toISOString().slice(0,10);const data=await api(`/api/public/scheduling/${slug}/slots?lesson_type_id=${x.id}&start=${iso(today)}&end=${iso(end)}`);const slots=data.slots.map(s=>new Date(s));$('#slots').replaceChildren(...(slots.length?slots.map(d=>button(d.toLocaleString([],{weekday:'short',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}),b=>chooseSlot(d,b))):[document.createTextNode('No times are currently available.')]))}function chooseSlot(d,b){start=d;[...$('#slots').children].forEach(n=>n.classList?.remove('active'));b.classList.add('active');$('#booking').classList.remove('hidden')}$('#booking').onsubmit=async e=>{e.preventDefault();$('#error').textContent='';const d=Object.fromEntries(new FormData(e.currentTarget));Object.assign(d,{lesson_type_id:type.id,starts_at:start.toISOString(),student_timezone:timezone});try{const result=await api(`/api/public/scheduling/${slug}/book`,{method:'POST',body:JSON.stringify(d)});$('#app').classList.add('hidden');$('#success').classList.remove('hidden');$('#success').textContent=`Lesson confirmed for ${new Date(result.starts_at).toLocaleString()}. You can close this page.`}catch(err){$('#error').textContent=err.message}};load().catch(e=>$('#error').textContent=e.message);
</script></body></html>"""
