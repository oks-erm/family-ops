"""Opt-in, local synthetic queue benchmark. Never calls models or sends Telegram messages."""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import AssistantInbox, Household, HouseholdMember, User
from app.db.repositories.assistant_queue import AssistantQueueRepository


async def run(args):
    url = make_url(os.environ.get("TEST_DATABASE_URL", "postgresql://invalid"))
    if url.host not in {"127.0.0.1", "localhost"} or not (url.database or "").endswith("_test"):
        raise SystemExit("Requires a disposable local TEST_DATABASE_URL ending _test.")
    if not 1 <= args.households <= 1000:
        raise SystemExit("Use 1..1000 synthetic households.")
    engine = create_async_engine(url, pool_size=2, max_overflow=0, pool_timeout=10)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    queue = AssistantQueueRepository(factory)
    users, families = [], []
    durations, order, active, peak, completed = [], {}, 0, 0, 0
    try:
        async with factory() as session:
            for _ in range(args.households):
                user = User(telegram_user_id=uuid4().int % 10**14, timezone="UTC")
                user.telegram_chat_id = user.telegram_user_id
                family = Household(name="Synthetic benchmark", invite_code=uuid4().hex.upper())
                session.add_all([user, family])
                await session.flush()
                session.add(HouseholdMember(user_id=user.id, household_id=family.id))
                users.append(user)
                families.append(family)
            await session.commit()
        offset = await queue.offset()
        updates = []
        for index, user in enumerate(users):
            for sequence in range(3):
                updates.append(
                    {
                        "update_id": offset + index * 3 + sequence,
                        "message": {
                            "message_id": sequence,
                            "date": int(datetime.now(UTC).timestamp()),
                            "from": {
                                "id": user.telegram_user_id,
                                "is_bot": False,
                                "first_name": "Synthetic",
                            },
                            "chat": {"id": user.telegram_chat_id, "type": "private"},
                            "text": "Synthetic queue load",
                        },
                    }
                )
        await queue.ingest(updates, v2_enabled=True)
        started = time.monotonic()

        async def worker():
            nonlocal active, peak, completed
            owner = uuid4()
            while completed < len(updates):
                begin = time.monotonic()
                job = await queue.claim(owner, global_limit=8)
                if job is None:
                    await asyncio.sleep(0.01)
                    continue
                assert job.chat_id in {u.telegram_chat_id for u in users}, (
                    "Use an idle test database"
                )
                sequence = job.payload["message"]["message_id"]
                assert sequence == order.get(job.chat_id, -1) + 1
                order[job.chat_id] = sequence
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)  # Simulated work outside the DB pool.
                await queue.finish(job.id, owner)
                active -= 1
                completed += 1
                durations.append((time.monotonic() - begin) * 1000)

        async with asyncio.timeout(120):
            await asyncio.gather(*(worker() for _ in range(12)))
        elapsed = time.monotonic() - started
        assert completed == args.households * 3 and peak <= 8
        durations.sort()
        report = {
            "synthetic": True,
            "households": args.households,
            "messages": completed,
            "pool_connections": 2,
            "worker_loops": 12,
            "global_limit": 8,
            "peak_active": peak,
            "elapsed_seconds": round(elapsed, 3),
            "messages_per_second": round(completed / elapsed, 2),
            "claim_work_finish_p50_ms": round(durations[len(durations) // 2], 1),
            "claim_work_finish_p95_ms": round(durations[int(len(durations) * 0.95)], 1),
            "per_channel_order": "passed",
            "model_calls": 0,
            "messages_sent": 0,
            "limitations": "Local queue throughput, not production capacity or model latency.",
        }
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    finally:
        async with factory() as session:
            await session.execute(
                delete(AssistantInbox).where(
                    AssistantInbox.chat_id.in_([u.telegram_chat_id for u in users])
                )
            )
            await session.execute(
                delete(Household).where(Household.id.in_([h.id for h in families]))
            )
            await session.execute(delete(User).where(User.id.in_([u.id for u in users])))
            await session.commit()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--households", type=int, default=100)
    parser.add_argument("--output", default="artifacts/assistant-queue-benchmark.json")
    asyncio.run(run(parser.parse_args()))
