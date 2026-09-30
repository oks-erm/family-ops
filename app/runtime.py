"""Independent household runtime roles. Web workers never start polling or scheduled jobs."""

import argparse
import asyncio
import json
import logging
import signal
import time
from contextlib import suppress
from pathlib import Path

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramUnauthorizedError
from sqlalchemy import text

from app.bot.main import create_bot, create_dispatcher
from app.config import get_settings
from app.db.repositories.assistant_queue import AssistantQueueRepository
from app.db.repositories.leases import LeaseBusy, LeaseRepository
from app.db.session import async_session_factory, engine
from app.services.conversation.worker import delivery_loop, worker_loop
from app.services.scheduler_service import SchedulerService

logger = logging.getLogger(__name__)


async def wait_for_schema():
    for _ in range(90):
        try:
            async with async_session_factory() as session:
                if (
                    await session.scalar(text("SELECT version_num FROM alembic_version"))
                    == "202609300002"
                ):
                    return
        except Exception as exc:
            logger.info("runtime_waiting_for_schema error_type=%s", type(exc).__name__)
        await asyncio.sleep(2)
    raise RuntimeError("The worker schema is not ready")


async def ingress(queue, bot, settings, progress):
    allowed = create_dispatcher().resolve_used_update_types()
    failures = 0
    while True:
        try:
            updates = await bot.get_updates(
                offset=await queue.offset(), timeout=10, request_timeout=20, allowed_updates=allowed
            )
        except (TelegramBadRequest, TelegramUnauthorizedError):
            raise RuntimeError("Telegram polling configuration was rejected") from None
        except (TelegramAPIError, OSError, TimeoutError) as exc:
            failures += 1
            logger.warning("telegram_ingress_retry error_type=%s", type(exc).__name__)
            await asyncio.sleep(min(30, 2 ** min(failures, 5)))
            continue
        failures = 0
        # Next getUpdates acknowledges this offset only after this transaction commits.
        await queue.ingest(
            [u.model_dump(mode="json", exclude_none=True) for u in updates],
            v2_enabled=settings.assistant_v2_enabled,
        )
        progress["at"] = time.time()


async def scheduler(bot, settings):
    service = SchedulerService(settings=settings, bot=bot)
    service.start()
    try:
        await asyncio.Event().wait()
    finally:
        await service.shutdown()


async def singleton(role, callback):
    while True:
        try:
            async with LeaseRepository(engine).held(f"runtime:{role}", seconds=60):
                await callback()
        except LeaseBusy:
            await asyncio.sleep(5)


async def maintain(queue, role, settings, progress):
    count = 0
    while True:
        # A real database round-trip: a dead database must not report worker health.
        await queue.offset()
        at = progress["at"] if role == "ingress" else time.time()
        Path(f"/tmp/family-{role}-health.json").write_text(json.dumps({"at": at}))
        if count % 360 == 0:
            await queue.expire_payloads(settings.assistant_history_days)
        count += 1
        await asyncio.sleep(10)


async def run(role):
    settings = get_settings()
    await wait_for_schema()
    bot = create_bot()
    if bot is None:
        raise RuntimeError("Telegram credentials are required for this runtime role")
    queue = AssistantQueueRepository(async_session_factory)
    progress = {"at": 0}
    parent = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, parent.cancel)
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(maintain(queue, role, settings, progress))
            if role == "ingress":
                group.create_task(singleton(role, lambda: ingress(queue, bot, settings, progress)))
            elif role == "scheduler":
                group.create_task(singleton(role, lambda: scheduler(bot, settings)))
            elif role == "delivery":
                group.create_task(delivery_loop(queue, bot))
            else:
                dispatcher = create_dispatcher()
                for _ in range(settings.assistant_worker_concurrency):
                    group.create_task(
                        worker_loop(queue, async_session_factory, settings, bot, dispatcher)
                    )
    finally:
        await bot.session.close()
        await engine.dispose()


def healthy(role):
    try:
        at = json.loads(Path(f"/tmp/family-{role}-health.json").read_text())["at"]
        return 0 <= time.time() - at < 60
    except (OSError, ValueError, KeyError, TypeError):
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["ingress", "worker", "delivery", "scheduler"])
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    if args.health:
        raise SystemExit(0 if healthy(args.role) else 1)
    logging.basicConfig(level=logging.INFO)
    with suppress(asyncio.CancelledError):
        asyncio.run(run(args.role))
