import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.db.repositories.calendar import CalendarRepository
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory
from app.services.calendar_service import CalendarService

router = APIRouter(prefix="/calendar", tags=["calendar"])

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def _calendar_redirect_uri() -> str:
    settings = get_settings()
    return f"{settings.public_base_url.rstrip('/')}/calendar/google/callback"


async def _dashboard_context(request: Request, session):
    google_email = request.session.get("google_email")
    if not google_email:
        return None, None
    dashboard_user = await UserRepository(session).get_by_google_email(google_email=str(google_email))
    if dashboard_user is None:
        request.session.clear()
        return None, None
    household = await HouseholdRepository(session).ensure_household_for_user(user=dashboard_user)
    return dashboard_user, household


@router.get("/google/start")
async def google_calendar_start(request: Request) -> RedirectResponse:
    settings = get_settings()
    if not settings.google_client_id:
        raise HTTPException(status_code=400, detail="GOOGLE_CLIENT_ID is not configured.")

    async with async_session_factory() as session:
        google_email = request.session.get("google_email")
        if not google_email:
            raise HTTPException(status_code=401, detail="Sign in to the dashboard before connecting Google Calendar.")
        user = await UserRepository(session).get_by_google_email(google_email=str(google_email))
        if user is None:
            raise HTTPException(status_code=404, detail="No onboarded user found.")

    state = secrets.token_urlsafe(24)
    request.session["calendar_oauth_state"] = state
    request.session["calendar_oauth_user_id"] = str(user.id)
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": _calendar_redirect_uri(),
            "response_type": "code",
            "scope": " ".join(GOOGLE_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{query}")


@router.get("/google/callback")
async def google_calendar_callback(
    request: Request,
    *,
    code: str = Query(...),
    state: str = Query(...),
) -> RedirectResponse:
    settings = get_settings()
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=400, detail="Google OAuth env vars are not configured.")

    async with async_session_factory() as session:
        expected_state = request.session.pop("calendar_oauth_state", None)
        user_id_raw = request.session.pop("calendar_oauth_user_id", None)
        if expected_state == state and user_id_raw:
            user_id = UUID(str(user_id_raw))
        else:
            # Legacy fallback for old links created before session-bound calendar OAuth.
            try:
                user_id = UUID(state)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid or expired calendar OAuth state.") from exc
        user = await UserRepository(session).get_by_id(user_id=user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found for OAuth state.")
        household = await HouseholdRepository(session).ensure_household_for_user(user=user)

        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": _calendar_redirect_uri(),
                    "grant_type": "authorization_code",
                },
            )
            response.raise_for_status()
            token_data = response.json()

        expires_in = int(token_data.get("expires_in") or 0)
        token_expires_at = datetime.now(UTC) + timedelta(seconds=expires_in) if expires_in else None
        await CalendarRepository(session).upsert_google_connection(
            user_id=user.id,
            household_id=household.id,
            external_account_id=(household.google_calendar_id or settings.google_calendar_id).strip() or "primary",
            access_token=token_data.get("access_token"),
            refresh_token=token_data.get("refresh_token"),
            token_expires_at=token_expires_at,
            scopes=GOOGLE_SCOPES,
        )
        await CalendarService(session).sync_google_connections(household_id=household.id)
        return RedirectResponse("/dashboard?calendar=connected")


@router.post("/ical")
async def add_ical_feed(request: Request, name: str, url: str) -> dict[str, str]:
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="The iCal feed URL must start with http:// or https://.")
    async with async_session_factory() as session:
        user, household = await _dashboard_context(request, session)
        if user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        feed = await CalendarRepository(session).add_ical_feed(
            user_id=user.id,
            household_id=household.id,
            name=name,
            url=url,
        )
        return {"id": str(feed.id), "status": "added"}


@router.post("/sync")
async def sync_calendars(request: Request) -> dict[str, int]:
    async with async_session_factory() as session:
        user, household = await _dashboard_context(request, session)
        if user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        service = CalendarService(session)
        ical_count = await service.sync_ical_feeds(household_id=household.id)
        google_count = await service.sync_google_connections(household_id=household.id)
        return {"ical_events": ical_count, "google_events": google_count}
