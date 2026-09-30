"""Explicit, bounded, synthetic model evaluation; never reads or writes household data.

python scripts/evaluate_assistant.py --live --model gpt-6-luna
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.conversation_model import ModelUnavailable, OpenAIConversationModel
from app.config import Settings
from app.schemas.conversation import TOOL_MODELS, tool_definitions
from app.services.conversation.service import INSTRUCTIONS

# Standard short-context USD per million tokens, verified 2026-09-30.
# https://developers.openai.com/api/docs/pricing
RATES = {
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-6.1-sol": (2.0, 0.10, 10.0),
    "gpt-6-astra": (10.0, 1.0, 50.0),  # Historical comparison; no default runtime route.
}


def usage_cost(model, usage):
    rate, cached_rate, output_rate = RATES[model]
    details = usage.get("input_tokens_details", {})
    cached = details.get("cached_tokens", 0)
    writes = details.get("cache_write_tokens", 0)
    return (
        (usage["input_tokens"] - cached) * rate
        + cached * cached_rate
        + writes * rate * 0.25
        + usage["output_tokens"] * output_rate
    ) / 1_000_000


CASES = [
    {
        "name": "shopping_typo",
        "text": "pls add oat mlk and eggs to shopping",
        "tool": "create_records",
        "field": "items",
        "size": 2,
    },
    {
        "name": "topic_switch",
        "history": [{"role": "assistant", "content": "What time do you start work tomorrow?"}],
        "text": "actually what's on my shopping list?",
        "tool": "list_records",
        "fields": {"kind": "shopping"},
    },
    {
        "name": "finance_followup",
        "history": [
            {"role": "user", "content": "How much did we spend on transport in September?"},
            {
                "role": "assistant",
                "content": "Recorded transport expenses total 42 EUR for September 2026.",
            },
        ],
        "text": "and last month?",
        "tool": "finance_query",
        "fields": {"start_date": "2026-08-01", "end_date": "2026-08-31", "kind": "expense"},
    },
    {"name": "ambiguous_reference", "text": "Move that to Friday", "clarify": True},
    {
        "name": "task_not_shopping",
        "text": "need to call the dentist tomorrow",
        "tool": "create_records",
        "field": "items",
        "size": 1,
        "item_kind": "task",
    },
    {
        "name": "income_capture",
        "text": "Record salary income of 1200 EUR today",
        "tool": "record_transaction",
        "fields": {"kind": "income", "currency": "EUR", "occurred_on": "2026-09-30"},
    },
]


async def run(args):
    if not args.live:
        raise SystemExit("Use --live to authorize the bounded synthetic API evaluation.")
    if args.model not in RATES:
        raise SystemExit("Add verified pricing for the requested model before a live evaluation.")
    settings = Settings(ASSISTANT_MAX_OUTPUT_TOKENS=1000)
    client = OpenAIConversationModel(settings)
    tools = tool_definitions()
    results, total = [], 0.0
    rate, _, output_rate = RATES[args.model]
    for case in CASES:
        inputs = [
            *case.get("history", []),
            {
                "role": "user",
                "content": "Current time: 2026-09-30T12:00:00+01:00. Timezone Europe/Lisbon.\n"
                + case["text"],
            },
        ]
        upper_input = len(json.dumps([INSTRUCTIONS, tools, inputs]).encode()) + 1024
        reserve = (upper_input * rate * 1.25 + 1000 * output_rate) / 1_000_000
        if total + reserve > args.max_usd:
            results.append({"case": case["name"], "status": "budget_stop"})
            break
        started = time.monotonic()
        try:
            response = await client.respond(
                model=args.model, instructions=INSTRUCTIONS, inputs=inputs, tools=tools
            )
        except ModelUnavailable as exc:
            results.append(
                {"case": case["name"], "status": "provider_unavailable", "error": str(exc)}
            )
            break
        usage = response.usage
        cost = usage_cost(args.model, usage)
        total += cost
        calls = response.calls
        passed = response.status == "completed"
        if case.get("clarify"):
            passed &= not calls and bool(response.text)
        else:
            passed &= len(calls) == 1 and calls[0].get("name") == case["tool"]
            if passed:
                try:
                    values = json.loads(calls[0]["arguments"])
                    TOOL_MODELS[case["tool"]][0].model_validate(values)
                    passed &= all(values.get(k) == v for k, v in case.get("fields", {}).items())
                    if "size" in case:
                        passed &= len(values.get(case["field"], [])) == case["size"]
                    if "item_kind" in case:
                        passed &= values["items"][0]["kind"] == case["item_kind"]
                except (ValueError, TypeError, KeyError):
                    passed = False
        results.append(
            {
                "case": case["name"],
                "status": "pass" if passed else "fail",
                "latency_ms": round((time.monotonic() - started) * 1000),
                "usage": usage,
                "estimated_usd": round(cost, 6),
                "response": response.output,
            }
        )
    report = {
        "model": args.model,
        "synthetic": True,
        "estimated_usd": round(total, 6),
        "pricing_date": "2026-09-30",
        "results": results,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "model": args.model,
                "passed": sum(r["status"] == "pass" for r in results),
                "attempted": len(results),
                "estimated_usd": report["estimated_usd"],
                "statuses": [r["status"] for r in results],
            }
        )
    )
    return len(results) == len(CASES) and all(item["status"] == "pass" for item in results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--max-usd", type=float, default=0.50)
    parser.add_argument("--output", default="artifacts/assistant-eval.json")
    sys.exit(0 if asyncio.run(run(parser.parse_args())) else 1)
