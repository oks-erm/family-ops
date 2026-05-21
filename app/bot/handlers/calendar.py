from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import get_settings
from app.db.repositories.calendar import CalendarRepository
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory

router = Router()


@router.message(Command("ical"))
async def add_ical_feed(message: Message) -> None:
    telegram_user = message.from_user
    if telegram_user is None:
        await message.answer("I could not identify your Telegram user.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Send /ical FEED_URL or /ical NAME FEED_URL.")
        return
    if len(parts) == 2:
        name = "Calendar"
        url = parts[1]
    else:
        name = parts[1]
        url = parts[2]
    if not url.startswith(("http://", "https://")):
        await message.answer("The iCal feed URL must start with http:// or https://.")
        return

    settings = get_settings()
    async with async_session_factory() as session:
        user = await UserRepository(session).upsert_telegram_user(
            telegram_user_id=telegram_user.id,
            telegram_chat_id=message.chat.id,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            username=telegram_user.username,
            timezone=settings.default_timezone,
        )
        household = await HouseholdRepository(session).ensure_household_for_user(user=user)
        await CalendarRepository(session).add_ical_feed(
            user_id=user.id,
            household_id=household.id,
            name=name,
            url=url,
        )
    await message.answer(f"Added iCal feed: {name}.")
