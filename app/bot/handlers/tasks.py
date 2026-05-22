from uuid import UUID

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.config import get_settings
from app.db.models import ActivityAction
from app.db.repositories.activity import ActivityRepository
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.tasks import TaskRepository
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory
from app.utils.datetime import now_in_timezone

router = Router()


@router.callback_query(F.data.startswith("task:"))
async def handle_task_action(callback: CallbackQuery) -> None:
    if callback.data is None or callback.from_user is None:
        await callback.answer("Missing task action.")
        return

    try:
        _, task_id_raw, action = callback.data.split(":", 2)
        task_id = UUID(task_id_raw)
    except ValueError:
        await callback.answer("Invalid task action.")
        return

    if action not in {"done", "skip", "move"}:
        await callback.answer("Unknown task action.")
        return

    settings = get_settings()
    async with async_session_factory() as session:
        user = await UserRepository(session).upsert_telegram_user(
            telegram_user_id=callback.from_user.id,
            telegram_chat_id=callback.message.chat.id if callback.message else callback.from_user.id,
            first_name=callback.from_user.first_name,
            last_name=callback.from_user.last_name,
            username=callback.from_user.username,
            timezone=settings.default_timezone,
        )
        household = await HouseholdRepository(session).ensure_household_for_user(user=user)
        today = now_in_timezone(user.timezone).date()
        task = await TaskRepository(session).apply_action(
            task_id=task_id,
            user_id=user.id,
            household_id=household.id,
            action=action,
            today=today,
        )
        if task is not None:
            action_text = {"done": "completed", "skip": "skipped", "move": "moved to tomorrow"}[action]
            await ActivityRepository(session).log(
                household_id=household.id,
                user_id=user.id,
                action=ActivityAction.updated,
                entity_type="task",
                entity_id=task.id,
                summary=f"Marked task {action_text}: {task.title}",
            )

    if task is None:
        await callback.answer("Task not found.")
        return

    action_text = {"done": "Done", "skip": "Skipped", "move": "Moved to tomorrow"}[action]
    await callback.answer(action_text)
    if callback.message is not None:
        await callback.message.edit_text(f"{action_text}: {task.title}")
