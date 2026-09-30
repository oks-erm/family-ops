from io import BytesIO
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
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


@router.message(F.document.mime_type.in_({"image/jpeg", "image/png", "image/webp"}))
@router.message(F.photo)
async def handle_receipt_photo(message: Message, bot: Bot) -> None:
    telegram_user = message.from_user
    if message.chat.type != "private":
        await message.answer("Please use our private chat for household images.", parse_mode=None)
        return
    media = message.photo[-1] if message.photo else message.document
    if telegram_user is None or media is None:
        await message.answer("I could not read this image. Please try again.", parse_mode=None)
        return
    mime_type = "image/jpeg" if message.photo else media.mime_type
    settings = get_settings()
    max_bytes = 20 * 1024 * 1024
    try:
        file = await bot.get_file(media.file_id)
        if (file.file_size or media.file_size or 0) > max_bytes:
            await message.answer("Please send an image smaller than 20 MB.", parse_mode=None)
            return
        buffer = BytesIO()
        await bot.download_file(file.file_path, destination=buffer)
        image_bytes = buffer.getvalue()
        if len(image_bytes) > max_bytes:
            await message.answer("Please send an image smaller than 20 MB.", parse_mode=None)
            return
    except (TelegramAPIError, OSError, TimeoutError):
        await message.answer(
            "I couldn't download this image. Please send it again.", parse_mode=None
        )
        return

    async with async_session_factory() as session:
        user = await UserRepository(session).upsert_telegram_user(
            telegram_user_id=telegram_user.id,
            telegram_chat_id=message.chat.id,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            username=telegram_user.username,
            timezone=settings.default_timezone,
        )
        if not user.family_dashboard_enabled:
            await message.answer(
                "The household assistant is unavailable for this account.", parse_mode=None
            )
            return
        finance_summary = await FinanceService(session, settings).extract_bank_screenshot(
            user_id=user.id,
            image_bytes=image_bytes,
            mime_type=mime_type,
            occurred_on=now_in_timezone(user.timezone).date(),
        )
        if finance_summary is not None:
            summary, pending_receipt_id = finance_summary, None
        else:
            summary, pending_receipt_id = await ReceiptService(
                session, settings
            ).extract_and_create_pending(
                user_id=user.id,
                telegram_chat_id=message.chat.id,
                image_bytes=image_bytes,
                image_path="",
                mime_type=mime_type,
            )

    if pending_receipt_id is None:
        await message.answer(summary, parse_mode=None)
        return

    await message.answer(
        summary,
        parse_mode=None,
        reply_markup=receipt_confirmation_keyboard(pending_receipt_id),
    )


@router.callback_query(F.data.startswith("receipt:"))
async def handle_receipt_confirmation(callback: CallbackQuery) -> None:
    if callback.data is None:
        await callback.answer("Missing receipt action.")
        return

    try:
        _, pending_receipt_id_raw, action = callback.data.split(":", 2)
        pending_receipt_id = UUID(pending_receipt_id_raw)
    except ValueError:
        await callback.answer("This receipt action is invalid.")
        return

    settings = get_settings()
    async with async_session_factory() as session:
        service = ReceiptService(session, settings)
        if action == "confirm":
            text = await service.confirm_pending_receipt(
                pending_receipt_id=pending_receipt_id, telegram_user_id=callback.from_user.id
            )
        elif action == "discard":
            text = await service.discard_pending_receipt(
                pending_receipt_id=pending_receipt_id, telegram_user_id=callback.from_user.id
            )
        else:
            await callback.answer("Unknown receipt action.")
            return

    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(text, parse_mode=None)
