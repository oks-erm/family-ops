from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import get_settings
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory
from app.services.finance_service import FinanceService
from app.services.receipt_service import ReceiptService
from app.utils.datetime import now_in_timezone

router = Router()


def receipt_confirmation_keyboard(pending_receipt_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Confirm",
                    callback_data=f"receipt:{pending_receipt_id}:confirm",
                ),
                InlineKeyboardButton(
                    text="Discard",
                    callback_data=f"receipt:{pending_receipt_id}:discard",
                ),
            ]
        ]
    )


@router.message(F.photo)
async def handle_receipt_photo(message: Message, bot: Bot) -> None:
    telegram_user = message.from_user
    if telegram_user is None or not message.photo:
        await message.answer("I could not read this photo. Please try again.")
        return

    settings = get_settings()
    largest_photo = message.photo[-1]
    file = await bot.get_file(largest_photo.file_id)

    with NamedTemporaryFile(prefix="receipt_", suffix=".jpg", delete=False) as tmp_file:
        tmp_path = Path(tmp_file.name)
        await bot.download_file(file.file_path, destination=tmp_file)

    image_bytes = tmp_path.read_bytes()

    async with async_session_factory() as session:
        user = await UserRepository(session).upsert_telegram_user(
            telegram_user_id=telegram_user.id,
            telegram_chat_id=message.chat.id,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            username=telegram_user.username,
            timezone=settings.default_timezone,
        )
        finance_summary = await FinanceService(session, settings).extract_bank_screenshot(
            user_id=user.id,
            image_bytes=image_bytes,
            mime_type="image/jpeg",
            occurred_on=now_in_timezone(user.timezone).date(),
        )
        if finance_summary is not None:
            summary, pending_receipt_id = finance_summary, None
        else:
            summary, pending_receipt_id = await ReceiptService(session, settings).extract_and_create_pending(
                user_id=user.id,
                telegram_chat_id=message.chat.id,
                image_bytes=image_bytes,
                image_path=str(tmp_path),
                mime_type="image/jpeg",
            )

    if pending_receipt_id is None:
        if finance_summary is not None and tmp_path.exists():
            tmp_path.unlink()
        await message.answer(summary)
        return

    await message.answer(
        summary,
        reply_markup=receipt_confirmation_keyboard(pending_receipt_id),
    )


@router.callback_query(F.data.startswith("receipt:"))
async def handle_receipt_confirmation(callback: CallbackQuery) -> None:
    if callback.data is None:
        await callback.answer("Missing receipt action.")
        return

    _, pending_receipt_id_raw, action = callback.data.split(":", 2)
    pending_receipt_id = UUID(pending_receipt_id_raw)

    settings = get_settings()
    async with async_session_factory() as session:
        service = ReceiptService(session, settings)
        if action == "confirm":
            text = await service.confirm_pending_receipt(pending_receipt_id=pending_receipt_id)
        elif action == "discard":
            text = await service.discard_pending_receipt(pending_receipt_id=pending_receipt_id)
        else:
            await callback.answer("Unknown receipt action.")
            return

    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(text)
