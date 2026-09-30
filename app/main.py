import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.routes.auth import router as auth_router
from app.routes.calendar import router as calendar_router
from app.routes.dashboard import router as dashboard_router

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Polling, conversation execution, reply delivery and timers have independent processes.
    yield


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


@app.get("/", response_model=None)
async def root(request: Request) -> dict[str, str] | RedirectResponse:
    return {
        "name": "Family Copilot",
        "status": "running",
        "health": "/health",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
