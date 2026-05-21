import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from app.bot.handlers import router
from app.config import get_settings

logger = logging.getLogger(__name__)


def create_bot() -> Bot | None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN is not configured; Telegram bot is disabled.")
        return None
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


async def start_polling() -> None:
    bot = create_bot()
    if bot is None:
        return

    dispatcher = create_dispatcher()
    await dispatcher.start_polling(bot)


def start_polling_in_background(bot: Bot | None = None) -> asyncio.Task[None] | None:
    bot = bot or create_bot()
    if bot is None:
        return None

    dispatcher = create_dispatcher()
    return asyncio.create_task(dispatcher.start_polling(bot))
