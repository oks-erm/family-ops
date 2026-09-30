"""Durable text processing; provider delivery is a separate worker, not a DB transaction."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import Update
from sqlalchemy import select

from app.db.models import AssistantConversation, AssistantTurn
from app.db.repositories.assistant_queue import UNCERTAIN
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.leases import LeaseLost
from app.db.repositories.users import UserRepository
from app.services.conversation.confirmations import confirmation_buttons, confirmation_callback
from app.services.conversation.service import ConversationService

logger = logging.getLogger(__name__)


async def process_text(payload, factory, settings, *, model=None):
    callback_text = confirmation_callback(payload)
    if callback_text:
        callback = payload["callback_query"]
        message = {
            **callback["message"],
            "text": callback_text,
            "message_id": f"callback:{payload['update_id']}",
        }
        sender = callback["from"]
    else:
        message, sender = payload["message"], payload["message"]["from"]
    if message["chat"]["type"] != "private":
        return "Please use our private chat for household requests."
    async with factory() as session:
        user = await UserRepository(session).upsert_telegram_user(
            telegram_user_id=sender["id"],
            telegram_chat_id=message["chat"]["id"],
            first_name=sender.get("first_name"),
            last_name=sender.get("last_name"),
            username=sender.get("username"),
            timezone=settings.default_timezone,
        )
        if message["text"] == "/last_reply":
            if not user.family_dashboard_enabled:
                return "The household assistant is unavailable for this account."
            household = await HouseholdRepository(session).ensure_household_for_user(user=user)
            result = await session.scalar(
                select(AssistantTurn.response)
                .join(
                    AssistantConversation, AssistantConversation.id == AssistantTurn.conversation_id
                )
                .where(
                    AssistantConversation.user_id == user.id,
                    AssistantConversation.household_id == household.id,
                    AssistantConversation.channel_key == f"telegram:{message['chat']['id']}",
                    AssistantTurn.status == "complete",
                    AssistantTurn.created_at
                    >= datetime.now(UTC) - timedelta(days=settings.assistant_history_days),
                )
                .order_by(AssistantTurn.created_at.desc())
                .limit(1)
            )
            return result or "There is no recent saved reply in this household."
        return await ConversationService(session, settings, model=model).handle(
            user_id=user.id,
            text=message["text"],
            channel_key=f"telegram:{message['chat']['id']}",
            message_key=str(message["message_id"]),
        )


async def process_job(queue, job, owner, factory, settings, bot, dispatcher, *, model=None):
    async def work():
        message = job.payload.get("message") or (job.payload.get("callback_query") or {}).get(
            "message"
        )
        if message and message.get("chat", {}).get("type") != "private":
            return "Please use our private chat for household requests."
        if job.replay_safe:
            if confirmation_callback(job.payload):
                with suppress(TelegramAPIError, OSError, TimeoutError):
                    await bot.answer_callback_query(job.payload["callback_query"]["id"])
            return await process_text(job.payload, factory, settings, model=model)
        await dispatcher.feed_update(bot, Update.model_validate(job.payload, context={"bot": bot}))
        return None

    async def heartbeat():
        while True:
            await asyncio.sleep(15)
            if not await queue.renew(job.id, owner):
                raise LeaseLost("Message lease lost")

    task, renewal = asyncio.create_task(work()), asyncio.create_task(heartbeat())
    try:
        async with asyncio.timeout(settings.assistant_turn_deadline_seconds):
            done, _ = await asyncio.wait([task, renewal], return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                await renewal
            response = await task
            await queue.finish(job.id, owner, response)
    except asyncio.CancelledError:
        # Shutdown leaves the durable job for recovery; uncertain writes are not acknowledged.
        raise
    except Exception as exc:
        logger.error("assistant_job_failed job=%s error_type=%s", job.id, type(exc).__name__)
        # Stop in-flight work before releasing the channel to the next job.
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        with suppress(LeaseLost):
            await queue.finish(job.id, owner, UNCERTAIN, error=type(exc).__name__)
    finally:
        task.cancel()
        renewal.cancel()
        for pending in (task, renewal):
            with suppress(asyncio.CancelledError, Exception):
                await pending


async def worker_loop(queue, factory, settings, bot, dispatcher):
    owner = uuid4()
    while True:
        job = await queue.claim(owner, global_limit=settings.assistant_global_concurrency)
        if job is None:
            await asyncio.sleep(0.5)
            continue
        await process_job(queue, job, owner, factory, settings, bot, dispatcher)


async def deliver_once(queue, bot, owner):
    message = await queue.claim_delivery(owner)
    if message is None:
        return False
    try:
        sent = await bot.send_message(
            chat_id=message.chat_id,
            text=message.body,
            parse_mode=None,
            request_timeout=20,
            reply_markup=confirmation_buttons(message.body),
        )
    except TelegramRetryAfter as exc:
        # Telegram explicitly rejected this request: retrying after its deadline is safe.
        await queue.delivered(
            message.id, owner, status="pending", delay=max(1, exc.retry_after), error="rate_limited"
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        await queue.delivered(message.id, owner, status="failed", error="telegram_rejected")
    except (TelegramAPIError, OSError, TimeoutError):
        await queue.delivered(message.id, owner, status="uncertain", error="unknown_delivery")
    else:
        await queue.delivered(message.id, owner, telegram_id=sent.message_id)
    return True


async def delivery_loop(queue, bot):
    owner = uuid4()
    while True:
        if not await deliver_once(queue, bot, owner):
            await asyncio.sleep(0.5)
