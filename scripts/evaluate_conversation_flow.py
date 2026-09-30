"""Opt-in live model + disposable PostgreSQL evaluation; creates only synthetic fixtures."""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.conversation_model import ModelUnavailable, OpenAIConversationModel
from app.config import Settings
from app.db.models import (
    AssistantConversation,
    FinancialTransaction,
    Household,
    HouseholdMember,
    ShoppingItem,
    ShoppingItemStatus,
    Task,
    User,
)
from app.services.conversation.service import ConversationService
from scripts.evaluate_assistant import RATES, usage_cost


class BoundedModel:
    def __init__(self, settings, limit):
        self.client = OpenAIConversationModel(settings)
        self.settings = settings
        self.limit = limit
        self.total = 0.0
        self.calls = []

    async def respond(self, **request):
        model = request["model"]
        if model not in RATES:
            raise ModelUnavailable("Evaluation has no verified pricing for this model")
        size = len(json.dumps(request).encode()) + 1024
        if size > 272000:
            raise ModelUnavailable("Evaluation is limited to short-context pricing")
        rate, _, output_rate = RATES[model]
        reserve = (size * rate * 1.25
                   + self.settings.assistant_max_output_tokens * output_rate) / 1_000_000
        if self.total + reserve > self.limit:
            raise ModelUnavailable("Synthetic evaluation dollar allowance exhausted")
        self.total += reserve  # Retain the reservation if the provider fails without usage.
        reply = await self.client.respond(**request)
        cost = usage_cost(model, reply.usage)
        self.total += cost - reserve
        self.calls.append({"model": model, "usage": reply.usage, "estimated_usd": cost})
        return reply


async def run(args):
    if not args.live or not 0 < args.max_usd <= 0.50:
        raise SystemExit("Use --live with a positive --max-usd no greater than 0.50.")
    url = make_url(os.environ.get("TEST_DATABASE_URL", "postgresql://invalid"))
    if url.host not in {"127.0.0.1", "localhost"} or not (url.database or "").endswith("_test"):
        raise SystemExit("Requires TEST_DATABASE_URL for an isolated local database ending _test.")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(ASSISTANT_MAX_OUTPUT_TOKENS=1000, ASSISTANT_MONTHLY_TOKEN_LIMIT=1000000)
    model = BoundedModel(settings, args.max_usd)
    results = []
    async with factory() as session:
        user = User(telegram_user_id=uuid4().int % 10**15, timezone="Europe/Lisbon")
        household = Household(name="Synthetic live evaluation", invite_code=uuid4().hex)
        session.add_all([user, household])
        await session.flush()
        session.add(HouseholdMember(user_id=user.id, household_id=household.id))
        await session.commit()
        user_id, household_id = user.id, household.id

    async def turn(name, text, check, *, message_key=None, zero_calls=False):
        before = len(model.calls)
        started = time.monotonic()
        # New session/service per message verifies history survives process-local state loss.
        async with factory() as session:
            response = await ConversationService(session, settings, model=model).handle(
                user_id=user_id, text=text, channel_key="synthetic-eval",
                message_key=message_key or name,
            )
            items = (await session.scalars(select(ShoppingItem).where(
                ShoppingItem.household_id == household_id,
                ShoppingItem.status == ShoppingItemStatus.pending,
            ))).all()
            tasks = (await session.scalars(select(Task).where(Task.user_id == user_id))).all()
            transactions = (await session.scalars(select(FinancialTransaction).where(
                FinancialTransaction.household_id == household_id,
            ))).all()
            conversation = await session.scalar(select(AssistantConversation).where(
                AssistantConversation.user_id == user_id,
            ))
            passed = check(response, items, tasks, transactions, conversation)
            calls = len(model.calls) - before
            if zero_calls:
                passed = passed and calls == 0
            results.append({
                "case": name, "status": "pass" if passed else "fail", "model_calls": calls,
                "latency_ms": round((time.monotonic() - started) * 1000), "response": response,
            })
            print(json.dumps({"case": name, "status": results[-1]["status"]}), flush=True)
            return response, conversation.pending

    try:
        first, _ = await turn(
            "shopping_typo", "pls add oat mlk and eggs to shopping",
            lambda r, items, *_: len(items) == 2,
        )
        await turn(
            "correction", "actually make the milk lactose-free milk",
            lambda r, items, *_: len(items) == 2 and any("lactose" in i.name for i in items),
        )
        tomorrow = datetime.now(ZoneInfo("Europe/Lisbon")).date() + timedelta(days=1)
        await turn(
            "task_topic", "I need to call the dentist tomorrow",
            lambda r, items, tasks, *_: len(tasks) == 1 and tasks[0].due_date == tomorrow,
        )
        await turn(
            "shopping_topic_return", "what's on the shopping list?",
            lambda r, items, tasks, *_: "lactose" in r.lower() and "egg" in r.lower()
            and len(items) == 2 and len(tasks) == 1,
        )
        _, pending = await turn(
            "removal_proposal", "remove the eggs",
            lambda r, items, tasks, tx, c: len(items) == 2 and bool(c.pending),
        )
        await turn(
            "confirmed_removal", "confirm " + (pending or {}).get("token", "missing"),
            lambda r, items, *_: len(items) == 1 and "lactose" in items[0].name,
            zero_calls=True,
        )
        await turn(
            "exact_read", "shopping list",
            lambda r, items, *_: len(items) == 1 and "lactose" in r.lower(), zero_calls=True,
        )
        await turn(
            "duplicate_after_restart", "pls add oat mlk and eggs to shopping",
            lambda r, items, *_: r == first and len(items) == 1,
            message_key="shopping_typo", zero_calls=True,
        )
        await turn(
            "income_capture", "Record salary income of 1200 EUR today",
            lambda r, items, tasks, tx, c: len(tx) == 1 and tx[0].amount == "1200",
        )
        await turn(
            "income_query", "how much income have I recorded this month?",
            lambda r, items, tasks, tx, c: len(tx) == 1 and "1200" in r.replace(",", "")
            and ("EUR" in r or "€" in r),
        )
    finally:
        report = {
            "synthetic": True, "estimated_usd": round(model.total, 8),
            "pricing_date": "2026-09-30", "includes_cache_write_premium": True,
            "results": results, "calls": model.calls,
        }
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
        await engine.dispose()
    print(json.dumps({"passed": sum(r["status"] == "pass" for r in results),
                      "attempted": len(results), "estimated_usd": report["estimated_usd"]}))
    return len(results) == 10 and all(r["status"] == "pass" for r in results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-usd", type=float, default=0.10)
    parser.add_argument("--output", default="artifacts/assistant-flow-evaluation.json")
    sys.exit(0 if asyncio.run(run(parser.parse_args())) else 1)
