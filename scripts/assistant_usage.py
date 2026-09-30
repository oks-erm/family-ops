"""Aggregate operational report; never prints messages, titles or household identifiers.

python scripts/assistant_usage.py --month 2026-09
"""

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import AssistantInbox, AssistantModelCall, AssistantOutbox
from app.db.session import async_session_factory


async def report(month):
    start = date.fromisoformat(month + "-01")
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(
                    AssistantModelCall.model,
                    AssistantModelCall.route,
                    AssistantModelCall.status,
                    func.count(),
                    func.sum(AssistantModelCall.input_tokens),
                    func.sum(AssistantModelCall.cached_tokens),
                    func.sum(AssistantModelCall.output_tokens),
                    func.avg(AssistantModelCall.duration_ms),
                    func.sum(AssistantModelCall.reserved_tokens),
                    func.sum(AssistantModelCall.estimated_usd),
                    func.sum(AssistantModelCall.cache_write_tokens),
                )
                .where(AssistantModelCall.created_at >= start, AssistantModelCall.created_at < end)
                .group_by(
                    AssistantModelCall.model, AssistantModelCall.route, AssistantModelCall.status
                )
            )
        ).all()
        queues = {}
        for table, name in ((AssistantInbox, "inbox"), (AssistantOutbox, "outbox")):
            queues[name] = dict(
                (
                    await session.execute(select(table.status, func.count()).group_by(table.status))
                ).all()
            )
    result = []
    for (
        model,
        route,
        status,
        calls,
        inputs,
        cached,
        outputs,
        latency,
        reserved,
        dollars,
        writes,
    ) in rows:
        result.append(
            {
                "model": model,
                "route": route,
                "status": status,
                "calls": calls,
                "input_tokens": inputs,
                "cached_tokens": cached,
                "output_tokens": outputs,
                "mean_latency_ms": round(float(latency)),
                "reserved_tokens": reserved,
                "estimated_usd": str(dollars) if dollars is not None else None,
                "cache_write_tokens": writes,
            }
        )
    print(
        json.dumps(
            {
                "month": month,
                "pricing_date": "2026-09-30",
                "groups": result,
                "pricing_basis": (
                    "Recorded estimates; old calls conservatively backfilled. "
                    "Unknown outcomes retain reservations."
                ),
                "queue_states": queues,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", required=True)
    asyncio.run(report(parser.parse_args().month))
