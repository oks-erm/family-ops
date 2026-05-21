from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def task_action_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Done", callback_data=f"task:{task_id}:done"),
                InlineKeyboardButton(text="Skip", callback_data=f"task:{task_id}:skip"),
            ],
            [InlineKeyboardButton(text="Move to tomorrow", callback_data=f"task:{task_id}:move")],
        ]
    )

