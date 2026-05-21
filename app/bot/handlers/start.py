from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.config import get_settings
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory

router = Router()


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    settings = get_settings()
    telegram_user = message.from_user
    if telegram_user is None:
        await message.answer("I could not read your Telegram user details. Please try again.")
        return

    async with async_session_factory() as session:
        repo = UserRepository(session)
        user = await repo.upsert_telegram_user(
            telegram_user_id=telegram_user.id,
            telegram_chat_id=message.chat.id,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            username=telegram_user.username,
            timezone=settings.default_timezone,
        )
        await HouseholdRepository(session).ensure_household_for_user(user=user)

    await message.answer(
        "You are onboarded. I will use Europe/Lisbon as your timezone for now.\n\n"
        "Use /invite to share household access with another user."
    )
