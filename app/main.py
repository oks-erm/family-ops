import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.bot.main import create_bot, start_polling_in_background
from app.config import get_settings
from app.routes.auth import router as auth_router
from app.routes.calendar import router as calendar_router
from app.routes.dashboard import router as dashboard_router
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
app.add_middleware(SessionMiddleware, secret_key=get_settings().dashboard_session_secret)
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(calendar_router)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "name": "Family Copilot",
        "status": "running",
        "health": "/health",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
