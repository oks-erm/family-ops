"""Aggregate operational report; never prints messages, titles or household identifiers.

python scripts/assistant_usage.py --month 2026-09
"""

import argparse
import asyncio
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import AssistantModelCall
from app.db.session import async_session_factory

# Verified standard short-context USD / million tokens, 2026-09-30.
# Rates are estimates; provider invoices and current pricing remain authoritative.
RATES = {
    "gpt-6-luna": (Decimal("0.10"), Decimal("0.01"), Decimal("0.50")),
    "gpt-6.1-sol": (Decimal("2"), Decimal("0.10"), Decimal("10")),
    "gpt-6-astra": (Decimal("10"), Decimal("1"), Decimal("50")),
}


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
                )
                .where(AssistantModelCall.created_at >= start, AssistantModelCall.created_at < end)
                .group_by(
                    AssistantModelCall.model, AssistantModelCall.route, AssistantModelCall.status
                )
            )
        ).all()
    result = []
    for model, route, status, calls, inputs, cached, outputs, latency, reserved in rows:
        rates = RATES.get(model)
        estimate = None
        upper_estimate = None
        if rates and status in {"completed", "incomplete"}:
            estimate = str(
                ((inputs - cached) * rates[0] + cached * rates[1] + outputs * rates[2]) / 1_000_000
            )
            # Historical usage rows do not separate cache writes from other uncached input.
            upper_estimate = str(
                ((inputs - cached) * rates[0] * Decimal("1.25")
                 + cached * rates[1] + outputs * rates[2]) / 1_000_000
            )
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
                "estimated_usd_min": estimate,
                "estimated_usd_max": upper_estimate,
            }
        )
    print(json.dumps({
        "month": month, "pricing_date": "2026-09-30", "groups": result,
        "pricing_basis": "Standard short context; range includes possible cache-write premium.",
    }, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", required=True)
    asyncio.run(report(parser.parse_args().month))
