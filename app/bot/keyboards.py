from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def task_action_keyboard(task_id: str, *, allow_move: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="Done", callback_data=f"task:{task_id}:done"),
            InlineKeyboardButton(text="Skip", callback_data=f"task:{task_id}:skip"),
        ]
    ]
    if allow_move:
        rows.append([InlineKeyboardButton(text="Move to tomorrow", callback_data=f"task:{task_id}:move")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

