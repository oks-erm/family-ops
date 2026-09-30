from aiogram import F, Router
from aiogram.types import Message

from app.config import get_settings
from app.bot.keyboards import task_action_keyboard
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory
from app.services.assistant_service import AssistantIntent, AssistantService
from app.services.conversation.service import ConversationService

router = Router()


def _strip_tasks_section(text: str) -> str:
    # When per-task action buttons are rendered separately, remove the inline
    # Tasks block from the plan text to avoid duplicate content.
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip().lower() == "tasks":
            return "\n".join(lines[:idx]).rstrip()
    return text.rstrip()


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text_message(message: Message) -> None:
    telegram_user = message.from_user
    if telegram_user is None or message.text is None:
        await message.answer("I could not read this message. Please try again.")
        return

    settings = get_settings()
    # Do this before upserting the user: a group message must not overwrite the
    # private chat destination used by household reminders.
    if settings.assistant_v2_enabled and message.chat.type != "private":
        await message.answer("Please use our private chat for household requests.")
        return
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

        if settings.assistant_v2_enabled:
            reply = await ConversationService(session, settings).handle(
                user_id=user.id, text=message.text,
                channel_key=f"telegram:{message.chat.id}",
                message_key=str(message.message_id),
            )
            # Model/user text is plain text, never trusted Telegram HTML.
            for start in range(0, len(reply), 3500):
                await message.answer(reply[start:start + 3500], parse_mode=None)
            return

        response = await AssistantService(session, settings).handle_text(
            user_id=user.id,
            text=message.text,
        )

    if response.intent == AssistantIntent.task_created:
        await message.answer(response.text)
        return

    task_actions = response.metadata.get("task_actions") if response.metadata else None
    text_to_send = response.text
    if isinstance(task_actions, list) and task_actions:
        text_to_send = _strip_tasks_section(text_to_send)

    await message.answer(text_to_send)
    if isinstance(task_actions, list):
        for item in task_actions:
            if not isinstance(item, dict) or not item.get("id") or not item.get("title"):
                continue
            await message.answer(
                str(item["title"]),
                reply_markup=task_action_keyboard(
                    str(item["id"]),
                    allow_move=bool(item.get("can_move", True)),
                ),
            )
