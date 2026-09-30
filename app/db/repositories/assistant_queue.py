"""PostgreSQL inbox/outbox with ordering, bounded concurrency and fenced completion."""

from datetime import timedelta

from sqlalchemy import exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from app.db.models import (
    AssistantHouseholdPolicy,
    AssistantInbox,
    AssistantOutbox,
    HouseholdMember,
    TransportCursor,
    User,
)
from app.db.repositories.leases import LeaseLost
from app.services.conversation.confirmations import confirmation_callback

UNCERTAIN = (
    "Processing was interrupted. Completed changes may already be saved. "
    "Please check the current data before requesting the action again."
)


class AssistantQueueRepository:
    def __init__(self, factory):
        self.factory = factory

    async def offset(self):
        async with self.factory() as session:
            return (
                await session.scalar(
                    select(TransportCursor.offset).where(TransportCursor.name == "telegram")
                )
                or 0
            )

    async def ingest(self, updates, *, v2_enabled):
        """Persist the whole ordered batch before Telegram can acknowledge its offset."""
        if not updates:
            return
        async with self.factory() as session:
            for payload in sorted(updates, key=lambda value: value["update_id"]):
                message = payload.get("message") or (payload.get("callback_query") or {}).get(
                    "message", {}
                )
                sender = (payload.get("callback_query") or {}).get("from") or message.get(
                    "from", {}
                )
                chat = message.get("chat", {})
                chat_id = chat.get("id")
                channel = (
                    f"telegram:{chat_id}"
                    if chat_id is not None
                    else f"update:{payload['update_id']}"
                )
                household_id = await session.scalar(
                    select(HouseholdMember.household_id)
                    .join(User, User.id == HouseholdMember.user_id)
                    .where(User.telegram_user_id == sender.get("id"))
                )
                if household_id:
                    await session.execute(
                        insert(AssistantHouseholdPolicy)
                        .values(household_id=household_id)
                        .on_conflict_do_nothing()
                    )
                message_text = message.get("text", "")
                replay_safe = bool(
                    v2_enabled
                    and payload.get("message")
                    and chat.get("type") == "private"
                    and message_text
                    and (not message_text.startswith("/") or message_text == "/last_reply")
                )
                replay_safe = replay_safe or bool(v2_enabled and confirmation_callback(payload))
                await session.execute(
                    insert(AssistantInbox)
                    .values(
                        update_id=payload["update_id"],
                        channel_key=channel,
                        chat_id=chat_id,
                        household_id=household_id,
                        payload=payload,
                        replay_safe=replay_safe,
                    )
                    .on_conflict_do_nothing(index_elements=[AssistantInbox.update_id])
                )
            statement = insert(TransportCursor).values(
                name="telegram", offset=max(u["update_id"] for u in updates) + 1
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[TransportCursor.name],
                    set_={
                        "offset": func.greatest(TransportCursor.offset, statement.excluded.offset)
                    },
                )
            )
            await session.commit()

    async def claim(self, owner, *, global_limit=12, lease_seconds=90):
        async with self.factory() as session:
            # A short transaction lock makes global and household concurrency counts atomic.
            await session.execute(text("SELECT pg_advisory_xact_lock(78230101)"))
            await self._recover(session)
            active = await session.scalar(
                select(func.count())
                .select_from(AssistantInbox)
                .where(AssistantInbox.status == "running")
            )
            if active >= global_limit:
                await session.commit()
                return None
            earlier = aliased(AssistantInbox)
            running = aliased(AssistantInbox)
            recent = aliased(AssistantInbox)
            active_count = (
                select(func.count())
                .select_from(running)
                .where(
                    running.household_id == AssistantInbox.household_id,
                    running.status == "running",
                )
                .correlate(AssistantInbox)
                .scalar_subquery()
            )
            minute_count = (
                select(func.count())
                .select_from(recent)
                .where(
                    recent.household_id == AssistantInbox.household_id,
                    recent.started_at > func.clock_timestamp() - timedelta(minutes=1),
                )
                .correlate(AssistantInbox)
                .scalar_subquery()
            )
            job = await session.scalar(
                select(AssistantInbox)
                .outerjoin(
                    AssistantHouseholdPolicy,
                    AssistantHouseholdPolicy.household_id == AssistantInbox.household_id,
                )
                .where(
                    AssistantInbox.status == "queued",
                    AssistantInbox.available_at <= func.clock_timestamp(),
                    ~exists(
                        select(earlier.id).where(
                            earlier.channel_key == AssistantInbox.channel_key,
                            earlier.id < AssistantInbox.id,
                            earlier.status.in_(["queued", "running"]),
                        )
                    ),
                    active_count < func.coalesce(AssistantHouseholdPolicy.max_concurrent, 2),
                    minute_count < func.coalesce(AssistantHouseholdPolicy.requests_per_minute, 30),
                )
                .order_by(
                    AssistantHouseholdPolicy.last_started_at.asc().nullsfirst(), AssistantInbox.id
                )
                .limit(1)
                .with_for_update(of=AssistantInbox, skip_locked=True)
            )
            if job:
                job.status, job.owner = "running", owner
                job.started_at = func.clock_timestamp()
                job.expires_at = func.clock_timestamp() + timedelta(seconds=lease_seconds)
                job.attempts += 1
                if job.household_id:
                    await session.execute(
                        update(AssistantHouseholdPolicy)
                        .where(AssistantHouseholdPolicy.household_id == job.household_id)
                        .values(last_started_at=func.clock_timestamp())
                    )
            await session.commit()
            return job

    async def _recover(self, session):
        expired = (
            await session.scalars(
                select(AssistantInbox)
                .where(
                    AssistantInbox.status == "running",
                    AssistantInbox.expires_at < func.clock_timestamp(),
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        ).all()
        for job in expired:
            if job.replay_safe and job.attempts < 3:
                # ConversationService returns a saved receipt, or warns without replaying writes.
                job.status = "queued"
            else:
                job.status, job.error_code = "uncertain", "worker_expired"
                await self._reply(session, job, UNCERTAIN)
            job.owner, job.expires_at = None, None

    async def renew(self, job_id, owner, seconds=90):
        async with self.factory() as session:
            result = await session.scalar(
                update(AssistantInbox)
                .where(
                    AssistantInbox.id == job_id,
                    AssistantInbox.owner == owner,
                    AssistantInbox.status == "running",
                    AssistantInbox.expires_at > func.clock_timestamp(),
                )
                .values(expires_at=func.clock_timestamp() + timedelta(seconds=seconds))
                .returning(AssistantInbox.id)
            )
            await session.commit()
            return result is not None

    async def finish(self, job_id, owner, response=None, *, error=None):
        async with self.factory() as session:
            job = await session.scalar(
                select(AssistantInbox)
                .where(
                    AssistantInbox.id == job_id,
                    AssistantInbox.owner == owner,
                    AssistantInbox.status == "running",
                    AssistantInbox.expires_at > func.clock_timestamp(),
                )
                .with_for_update()
            )
            if job is None:
                raise LeaseLost("The message lease expired")
            # /start and /join can change membership while later messages are already queued.
            sender = (job.payload.get("callback_query") or {}).get("from") or (
                job.payload.get("message") or {}
            ).get("from", {})
            household_id = await session.scalar(
                select(HouseholdMember.household_id)
                .join(User, User.id == HouseholdMember.user_id)
                .where(User.telegram_user_id == sender.get("id"))
            )
            if household_id:
                await session.execute(
                    insert(AssistantHouseholdPolicy)
                    .values(household_id=household_id)
                    .on_conflict_do_nothing()
                )
            await session.execute(
                update(AssistantInbox)
                .where(
                    AssistantInbox.channel_key == job.channel_key, AssistantInbox.status == "queued"
                )
                .values(household_id=household_id)
            )
            await self._reply(session, job, response)
            job.status = "uncertain" if error else "complete"
            job.error_code = error
            job.payload = {}
            job.owner, job.expires_at = None, None
            await session.commit()

    @staticmethod
    async def _reply(session, job, response):
        if not response or job.chat_id is None:
            return
        for part, start in enumerate(range(0, len(response), 3500)):
            await session.execute(
                insert(AssistantOutbox)
                .values(
                    inbox_id=job.id,
                    part=part,
                    chat_id=job.chat_id,
                    body=response[start : start + 3500],
                )
                .on_conflict_do_nothing(constraint="uq_assistant_delivery_part")
            )

    async def claim_delivery(self, owner):
        async with self.factory() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(78230102)"))
            # An expired send may have reached Telegram. Never blindly resend it.
            await session.execute(
                update(AssistantOutbox)
                .where(
                    AssistantOutbox.status == "sending",
                    AssistantOutbox.expires_at < func.clock_timestamp(),
                )
                .values(status="uncertain", error_code="delivery_expired")
            )
            failed = aliased(AssistantOutbox)
            await session.execute(
                update(AssistantOutbox)
                .where(
                    AssistantOutbox.status == "pending",
                    exists(
                        select(failed.id).where(
                            failed.inbox_id == AssistantOutbox.inbox_id,
                            failed.status.in_(["uncertain", "failed"]),
                        )
                    ),
                )
                .values(status="failed", error_code="prior_part_failed")
            )
            earlier = aliased(AssistantOutbox)
            message = await session.scalar(
                select(AssistantOutbox)
                .where(
                    AssistantOutbox.status == "pending",
                    AssistantOutbox.available_at <= func.clock_timestamp(),
                    ~exists(
                        select(earlier.id).where(
                            earlier.chat_id == AssistantOutbox.chat_id,
                            earlier.id < AssistantOutbox.id,
                            earlier.status.in_(["pending", "sending"]),
                        )
                    ),
                )
                .order_by(AssistantOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if message:
                message.status, message.owner = "sending", owner
                message.expires_at = func.clock_timestamp() + timedelta(seconds=60)
            await session.commit()
            return message

    async def delivered(
        self, message_id, owner, *, telegram_id=None, status="sent", delay=0, error=None
    ):
        async with self.factory() as session:
            result = await session.execute(
                update(AssistantOutbox)
                .where(
                    AssistantOutbox.id == message_id,
                    AssistantOutbox.owner == owner,
                    AssistantOutbox.status == "sending",
                    AssistantOutbox.expires_at > func.clock_timestamp(),
                )
                .values(
                    status=status,
                    message_id=telegram_id,
                    error_code=error,
                    available_at=func.clock_timestamp() + timedelta(seconds=delay),
                    owner=None,
                    expires_at=None,
                    **({"body": ""} if status == "sent" else {}),
                )
            )
            if not result.rowcount:
                raise LeaseLost("The delivery lease expired")
            await session.commit()

    async def expire_payloads(self, days):
        async with self.factory() as session:
            cutoff = func.clock_timestamp() - timedelta(days=days)
            await session.execute(
                update(AssistantInbox)
                .where(
                    AssistantInbox.created_at < cutoff,
                    AssistantInbox.status.in_(["complete", "uncertain"]),
                    AssistantInbox.payload != {},
                )
                .values(payload={})
            )
            await session.execute(
                update(AssistantOutbox)
                .where(
                    AssistantOutbox.created_at < cutoff,
                    AssistantOutbox.status.in_(["sent", "uncertain", "failed"]),
                    AssistantOutbox.body != "",
                )
                .values(body="")
            )
            await session.commit()
