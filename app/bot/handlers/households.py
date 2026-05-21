from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import get_settings
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory

router = Router()


@router.message(Command("invite"))
async def handle_invite(message: Message) -> None:
    telegram_user = message.from_user
    if telegram_user is None:
        await message.answer("I could not read your Telegram user details. Please try again.")
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

    await message.answer(
        f"Household invite code: {household.invite_code}\n\n"
        f"Another user can join with: /join {household.invite_code}"
    )


@router.message(Command("dashboard_link"))
async def handle_dashboard_link(message: Message) -> None:
    telegram_user = message.from_user
    if telegram_user is None:
        await message.answer("I could not read your Telegram user details. Please try again.")
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
        await HouseholdRepository(session).ensure_household_for_user(user=user)
        token = await UserRepository(session).create_dashboard_link_token(user_id=user.id)

    link = f"{settings.public_base_url.rstrip('/')}/auth/google/start?link_token={token}"
    await message.answer(
        "Open this link to connect your Google account to the household dashboard. "
        "It expires in 30 minutes:\n\n"
        f"{link}"
    )


@router.message(Command("join"))
async def handle_join(message: Message) -> None:
    telegram_user = message.from_user
    if telegram_user is None or message.text is None:
        await message.answer("I could not read your Telegram user details. Please try again.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Send /join followed by the household invite code.")
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
        household = await HouseholdRepository(session).join_by_invite_code(
            user=user,
            invite_code=parts[1],
        )

    if household is None:
        await message.answer("I could not find a household with that invite code.")
        return

    await message.answer(f"Joined household: {household.name}.")
