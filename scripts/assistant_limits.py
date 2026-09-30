"""Operator-only household policy configuration. Reads by default; writes require --apply."""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import AssistantHouseholdPolicy, AssistantModelCall, Household
from app.db.session import async_session_factory


def dollar_limit(value):
    if value == "none":
        return None
    try:
        amount = Decimal(value)
        if not amount.is_finite() or not 0 <= amount <= 10000:
            raise ValueError
        return amount.quantize(Decimal("0.00000001"))
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError("Use none or a dollar amount from 0 to 10000") from None


async def run(args):
    async with async_session_factory() as session:
        if await session.get(Household, args.household_id) is None:
            raise ValueError("Household not found")
        changes = {}
        if args.monthly_usd is not None:
            changes["monthly_usd_limit"] = dollar_limit(args.monthly_usd)
        if args.monthly_tokens is not None:
            if not 0 <= args.monthly_tokens <= 100000000:
                raise ValueError("Token limit must be between 0 and 100000000")
            changes["monthly_token_limit"] = args.monthly_tokens
        for key, value, upper in [
            ("max_concurrent", args.concurrency, 16),
            ("requests_per_minute", args.requests_per_minute, 1000),
        ]:
            if value is not None:
                if not 1 <= value <= upper:
                    raise ValueError("Invalid concurrency or rate limit")
                changes[key] = value
        if changes.get("monthly_usd_limit") is not None:
            month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            unknown = await session.scalar(
                select(func.count())
                .select_from(AssistantModelCall)
                .where(
                    AssistantModelCall.household_id == args.household_id,
                    AssistantModelCall.created_at >= month,
                    AssistantModelCall.estimated_usd.is_(None),
                )
            )
            if unknown:
                raise ValueError(
                    "Reconcile this month's unpriced calls before enabling a dollar cap"
                )
        if changes and args.apply:
            statement = insert(AssistantHouseholdPolicy).values(
                household_id=args.household_id, **changes
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[AssistantHouseholdPolicy.household_id], set_=changes
                )
            )
            await session.commit()
        policy = await session.get(AssistantHouseholdPolicy, args.household_id)
        print(
            json.dumps(
                {
                    "applied": bool(changes and args.apply),
                    "proposed": changes,
                    "current": None
                    if policy is None
                    else {
                        key: getattr(policy, key)
                        for key in (
                            "monthly_token_limit",
                            "monthly_usd_limit",
                            "max_concurrent",
                            "requests_per_minute",
                        )
                    },
                },
                default=str,
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--household-id", type=UUID, required=True)
    parser.add_argument("--monthly-usd", help="USD estimate cap, or none to disable")
    parser.add_argument("--monthly-tokens", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--requests-per-minute", type=int)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(parser.parse_args()))
