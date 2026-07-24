import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app.bot.main import create_bot, start_polling_in_background
from app.config import get_settings
from app.db.models import SchedulingProfile
from app.db.session import async_session_factory
from app.routes.auth import router as auth_router
from app.routes.calendar import router as calendar_router
from app.routes.dashboard import router as dashboard_router
from app.routes.scheduling import router as scheduling_router
from app.services.scheduler_service import SchedulerService

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    bot = create_bot()
    scheduler_service = SchedulerService(settings=settings, bot=bot)
    polling_task = start_polling_in_background(bot)

    scheduler_service.start()
    app.state.scheduler_service = scheduler_service
    app.state.polling_task = polling_task
    app.state.bot = bot

    try:
        yield
    finally:
        await scheduler_service.shutdown()
        if polling_task is not None:
            polling_task.cancel()
        if bot is not None:
            await bot.session.close()


app = FastAPI(title="Family Copilot", lifespan=lifespan)
settings = get_settings()
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.dashboard_session_secret,
    session_cookie=settings.session_cookie_name,
    domain=settings.session_cookie_domain,
    https_only=settings.app_env.casefold() == "production",
    same_site="lax",
)
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(calendar_router)
app.include_router(scheduling_router)


_SCHEDULING_HOST_PATHS = (
    "/",
    "/health",
    "/schedule/manage",
    "/auth/google/start",
    "/auth/google/callback",
    "/auth/not-invited",
    "/auth/logout",
    "/calendar/google/start",
    "/calendar/google/callback",
)
_SCHEDULING_HOST_PREFIXES = (
    "/book/",
    "/api/scheduling/",
    "/api/public/scheduling/",
)


def scheduling_host_allows_path(path: str) -> bool:
    return path in _SCHEDULING_HOST_PATHS or path.startswith(_SCHEDULING_HOST_PREFIXES)


@app.middleware("http")
async def isolate_scheduling_host(request: Request, call_next):
    scheduling_host = urlsplit(get_settings().scheduling_public_base_url or "").hostname
    if (
        scheduling_host
        and (request.url.hostname or "").casefold() == scheduling_host.casefold()
        and not scheduling_host_allows_path(request.url.path)
    ):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not found."}, status_code=404)
        return RedirectResponse("/schedule/manage", status_code=303)
    return await call_next(request)


@app.get("/", response_model=None)
async def root(request: Request) -> dict[str, str] | RedirectResponse:
    settings = get_settings()
    scheduling_host = urlsplit(settings.scheduling_public_base_url or "").hostname
    if scheduling_host and request.url.hostname == scheduling_host:
        if request.session.get("google_email"):
            return RedirectResponse("/schedule/manage", status_code=303)
        async with async_session_factory() as session:
            result = await session.execute(
                select(SchedulingProfile)
                .where(SchedulingProfile.is_active.is_(True))
                .order_by(SchedulingProfile.created_at)
                .limit(1)
            )
            profile = result.scalar_one_or_none()
        if profile is not None:
            return RedirectResponse(f"/book/{profile.slug}", status_code=303)
        return RedirectResponse("/schedule/manage", status_code=303)
    return {
        "name": "Family Copilot",
        "status": "running",
        "health": "/health",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
