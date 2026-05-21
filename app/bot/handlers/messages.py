from aiogram import F, Router
from aiogram.types import Message

from app.config import get_settings
from app.bot.keyboards import task_action_keyboard
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory
from app.services.assistant_service import AssistantIntent, AssistantService

router = Router()


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text_message(message: Message) -> None:
    telegram_user = message.from_user
    if telegram_user is None or message.text is None:
        await message.answer("I could not read this message. Please try again.")
        return

    settings = get_settings()
    async with async_session_factory() as session:
        user_repo = UserRepository(session)
        user = await user_repo.upsert_telegram_user(
            telegram_user_id=telegram_user.id,
            telegram_chat_id=message.chat.id,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            username=telegram_user.username,
            timezone=settings.default_timezone,
        )

        response = await AssistantService(session, settings).handle_text(
            user_id=user.id,
            text=message.text,
        )

    if response.intent == AssistantIntent.task_created and response.metadata:
        await message.answer(
            response.text,
            reply_markup=task_action_keyboard(response.metadata["task_id"]),
        )
        return

    await message.answer(response.text)
