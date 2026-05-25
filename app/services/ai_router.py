import json
import logging
from base64 import b64encode
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class AiTaskWeight(StrEnum):
    light = "light"
    heavy = "heavy"


class AiProvider(StrEnum):
    deterministic = "deterministic"
    ollama = "ollama"
    gemini = "gemini"
    openai = "openai"
    disabled = "disabled"


@dataclass(frozen=True)
class AiJsonResult:
    provider: AiProvider
    data: dict[str, Any] | None
    error: str | None = None


class AiRouter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def classify_light_intent(self, *, text: str) -> AiJsonResult:
        provider = self._provider_for(AiTaskWeight.light)
        if provider in {AiProvider.deterministic, AiProvider.disabled}:
            return AiJsonResult(provider=provider, data=None)

        prompt = (
            "You are the cheap conversational/intent layer for a Telegram household assistant. "
            "Return only compact JSON. Do not use markdown. Do not invent facts.\n"
            "Classify the user's current message independently. The app may have asked a previous "
            "question, but do not treat this message as an answer unless it actually semantically "
            "answers that question.\n"
            "Keys: intent, confidence, reply, item, title, date_ref, store_name, target, "
            "source, start_time, end_time, kind, category, duration_minutes.\n"
            "Allowed intents: smalltalk, capability_question, add_shopping_item, going_to_store, "
            "shopping_summary, task_created, planning_query, planning_note, fixed_event, "
            "work_hours_update, sleep_window_update, mark_task_done, mark_shopping_purchased, "
            "remove_item, move_item, finance_transaction, expense_query, clarify, unknown.\n"
            "Rules:\n"
            "- Messages starting with 'need' or 'buy' are ALWAYS add_shopping_item (confidence 0.9+) "
            "unless the remainder is clearly a verb phrase like 'fix the sink', 'call dentist', 'clean house'. "
            "Any noun — food, drink, household item, personal care, electronics, clothing, cosmetics — is a product. "
            "Never use clarify for 'need X' or 'buy X' where X is a noun, even unfamiliar or foreign words.\n"
            "- Use add_shopping_item only for concrete products to buy, never activities.\n"
            "- Compound product names (e.g. salt and pepper, mac and cheese, bread and butter) are "
            "ONE item — never split them into two. Only list multiple items in the item field as "
            "comma-separated when the user clearly lists distinct unrelated products.\n"
            "- When user wants to order something online (e.g. 'order X online', 'buy X from Amazon'), "
            "set store_name to 'online'.\n"
            "- Use task_created for activities/actions like cook dinner, play drums, call dentist, "
            "exercise, read, clean, watch a movie, especially when the user says task or todo.\n"
            "- Use planning_query when the user asks what to do, asks for a plan, asks what is planned, "
            "or asks for tasks for today/tomorrow/week.\n"
            "- Use planning_note for general context that affects planning but has no exact time.\n"
            "- Use fixed_event for schedule changes with a title and time range, e.g. wedding 18:00-23:00.\n"
            "- Use work_hours_update when work start and finish times are given.\n"
            "- Use sleep_window_update for wake/go-to-bed/sleep time changes; kind must be wake or sleep.\n"
            "- Use mark_task_done when the user says they already did/completed a task or routine.\n"
            "- Use remove_item to delete from shopping/tasks/plans; target should be shopping, task, plan, or any.\n"
            "- Use move_item when (a) moving between shopping/task/plan, (b) rescheduling to today/tomorrow, "
            "OR (c) reassigning a shopping item to a different store — set target=STORE_NAME "
            "(e.g. 'move beef to Lidl' → intent=move_item, item=beef, target=Lidl; "
            "'move broccoli to anywhere' → target=anywhere).\n"
            "- Use expense_query when the user asks about spending or income. "
            "Set kind='income' for income/salary/earnings queries, kind='spend' for expense queries (default). "
            "Set category to the specific thing they ask about (e.g. 'food', 'commute', 'transport', 'entertainment'), "
            "or omit for general totals. ALWAYS use English for category (e.g. 'commute' not 'transportes'). "
            "Set date_ref to the time period: 'this month', 'last month', 'this week', 'this year', "
            "'last year', a month name like 'April' or 'April 2025', or a year like '2024'.\n"
            "- The user's message may be in any language. "
            "Return ALL field values in English EXCEPT reply (which must match the user's language).\n"
            "- For smalltalk/capability_question/clarify, include a short natural reply.\n"
            "- Only use clarify when the message has NO shopping/buy prefix and is genuinely ambiguous between task and plan.\n"
            "- confidence is 0..1. date_ref can be: today, tonight, tomorrow, week, this month, last month, "
            "this year, last year, a month name (e.g. April, April 2025), or a year (e.g. 2024). Empty if not relevant.\n"
            "- Times must be HH:MM 24-hour when present.\n\n"
            f"Message: {text}"
        )

        if provider == AiProvider.ollama:
            return await self._ollama_json(prompt=prompt)
        if provider == AiProvider.gemini:
            return await self._gemini_json(prompt=prompt)
        if provider == AiProvider.openai:
            logger.warning("OpenAI configured for a light task; skipping to avoid unnecessary cost.")
            return AiJsonResult(provider=provider, data=None, error="openai_light_tasks_disabled")

        return AiJsonResult(provider=provider, data=None)

    async def extract_receipt(self, *, image_bytes: bytes, mime_type: str) -> AiJsonResult:
        gemini_result = await self._gemini_receipt_json(image_bytes=image_bytes, mime_type=mime_type)
        if gemini_result.data is not None:
            return gemini_result

        if self._provider_for(AiTaskWeight.heavy) == AiProvider.openai and self.settings.openai_api_key:
            logger.info("Gemini receipt extraction failed; OpenAI fallback is configured but not implemented yet.")
            return AiJsonResult(
                provider=AiProvider.openai,
                data=None,
                error="openai_receipt_fallback_not_implemented",
            )

        return gemini_result

    async def extract_bank_transactions(self, *, image_bytes: bytes, mime_type: str) -> AiJsonResult:
        return await self._gemini_bank_json(image_bytes=image_bytes, mime_type=mime_type)

    def _provider_for(self, weight: AiTaskWeight) -> AiProvider:
        raw_provider = (
            self.settings.ai_light_provider
            if weight == AiTaskWeight.light
            else self.settings.ai_heavy_provider
        )
        try:
            return AiProvider(raw_provider.strip().lower())
        except ValueError:
            logger.warning("Unknown AI provider configured: %s", raw_provider)
            return AiProvider.disabled

    async def _ollama_json(self, *, prompt: str) -> AiJsonResult:
        url = f"{self.settings.ollama_base_url.rstrip('/')}/api/generate"
        payload = {
            "model": self.settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.ai_request_timeout_seconds) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
            body = response.json()
            return AiJsonResult(
                provider=AiProvider.ollama,
                data=json.loads(body.get("response", "{}")),
            )
        except Exception as exc:
            logger.info("Ollama light AI call failed: %s", exc)
            return AiJsonResult(provider=AiProvider.ollama, data=None, error=str(exc))

    async def classify_food_items(self, item_names: list[str]) -> dict[str, str]:
        """Batch-classify grocery item names into food categories via AI.

        Returns a mapping of item_name -> category.
        Falls back to an empty dict on failure (caller handles fallback).
        """
        if not item_names:
            return {}
        categories = (
            "Protein, Veg, Fruit & Nuts, Bread & Carbs, "
            "Dairy, Sweets, Snacks, Drinks, Other Food"
        )
        items_json = json.dumps(item_names, ensure_ascii=False)
        prompt = (
            "You are a grocery item categorizer for a household finance app. "
            "Classify each item name from Portuguese supermarket receipts into exactly one category. "
            f"Categories: {categories}.\n"
            "Rules:\n"
            "- Protein: meat, fish, seafood, eggs, cold cuts, sausages\n"
            "- Veg: all vegetables (fresh, frozen, canned)\n"
            "- Fruit & Nuts: all fruit, nuts, seeds, dried fruit\n"
            "- Bread & Carbs: bread, rice, pasta, oats, flour, cereals, legumes, grains\n"
            "- Dairy: milk, cheese, yogurt, butter, cream, eggs substitutes\n"
            "- Sweets: chocolate, candy, ice cream, cakes, pastries, honey, jams\n"
            "- Snacks: crisps, chips, popcorn, crackers, snack bars, aperitivo\n"
            "- Drinks: water, juice, soft drinks, beer, wine, spirits, coffee, tea\n"
            "- Other Food: oils, condiments, sauces, spices, cleaning products, "
            "  hygiene, or anything not fitting above\n"
            "Items may be in Portuguese, English, or brand names. "
            "Return only compact JSON with no markdown: "
            '{"item name as given": "Category", ...}\n'
            f"Items: {items_json}"
        )
        result = await self._gemini_json(prompt=prompt)
        if result.data and isinstance(result.data, dict):
            return {k: v for k, v in result.data.items() if isinstance(v, str)}
        return {}

    async def answer_finance_question(
        self,
        *,
        question: str,
        tx_rows: list[dict[str, str]],
        item_rows: list[dict[str, str]],
        breakdown: bool = False,
    ) -> str:
        """Answer a natural-language finance question from raw transaction + receipt data.

        tx_rows: list of {date, category, description, amount, currency, type}
        item_rows: list of {date, store, name, amount}
        breakdown: if True, include an itemised list; otherwise summary only.
        Returns a short plain-text answer.
        """
        if not self.settings.gemini_api_key:
            return "AI not configured — cannot answer query."

        tx_lines = "\n".join(
            f"{r['date']} | {r['type']} | {r['category']} | {r['description']} | {r['amount']} {r['currency']}"
            for r in tx_rows[:300]
        ) or "(no bank transactions in this period)"

        item_lines = "\n".join(
            f"{r['date']} | {r['store']} | {r['name']} | {r['amount']}"
            for r in item_rows[:500]
        ) or "(no scanned receipt items in this period)"

        if breakdown:
            detail_instruction = "Show total, number of transactions, then list every matching entry (date, description, amount)."
        else:
            detail_instruction = "Reply with ONE concise line: total amount and number of transactions. No list, no examples."

        # Detect the question language to give Gemini an explicit instruction.
        pt_markers = ("quanto", "gastamos", "gastámos", "despesa", "mês", "este", "esta", "ano")
        lang_instruction = (
            "Reply in Portuguese."
            if any(m in question.lower() for m in pt_markers)
            else "Reply in English."
        )

        prompt = (
            "You are a household finance assistant. Answer the question below using ONLY "
            "the data provided. Do not invent amounts. Sum amounts yourself.\n"
            f"{detail_instruction}\n"
            f"{lang_instruction}\n\n"
            f"Question: {question}\n\n"
            "Bank transactions (date | type | category | description | amount currency):\n"
            f"{tx_lines}\n\n"
            "Scanned receipt line items (date | store | item name | amount):\n"
            f"{item_lines}"
        )

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.settings.gemini_model}:generateContent"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0},
        }
        headers = {"x-goog-api-key": self.settings.gemini_api_key}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
            candidate = response.json()["candidates"][0]
            return candidate["content"]["parts"][0]["text"].strip()
        except Exception as exc:
            logger.info("Gemini finance question failed: %s", exc)
            return f"Could not answer query: {exc}"

    async def _gemini_json(self, *, prompt: str) -> AiJsonResult:
        if not self.settings.gemini_api_key:
            return AiJsonResult(provider=AiProvider.gemini, data=None, error="missing_gemini_api_key")

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.settings.gemini_model}:generateContent"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        }
        headers = {"x-goog-api-key": self.settings.gemini_api_key}
        try:
            async with httpx.AsyncClient(timeout=self.settings.ai_request_timeout_seconds) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
            candidate = response.json()["candidates"][0]
            text = candidate["content"]["parts"][0]["text"]
            return AiJsonResult(provider=AiProvider.gemini, data=json.loads(text))
        except Exception as exc:
            logger.info("Gemini light AI call failed: %s", exc)
            return AiJsonResult(provider=AiProvider.gemini, data=None, error=str(exc))

    async def _gemini_receipt_json(self, *, image_bytes: bytes, mime_type: str) -> AiJsonResult:
        if not self.settings.gemini_api_key:
            return AiJsonResult(provider=AiProvider.gemini, data=None, error="missing_gemini_api_key")

        prompt = (
            "Extract this grocery receipt. Return only JSON with keys: shop_name, "
            "purchased_at as YYYY-MM-DD or null, total_amount as string or null, "
            "currency as string or null, and items. items must be an array of objects "
            "with keys name, quantity, total_amount, category. Use null when unknown."
        )
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.settings.gemini_model}:generateContent"
        )
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": b64encode(image_bytes).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        }
        headers = {"x-goog-api-key": self.settings.gemini_api_key}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
            candidate = response.json()["candidates"][0]
            text = candidate["content"]["parts"][0]["text"]
            return AiJsonResult(provider=AiProvider.gemini, data=json.loads(text))
        except Exception as exc:
            logger.info("Gemini receipt extraction failed: %s", exc)
            return AiJsonResult(provider=AiProvider.gemini, data=None, error=str(exc))

    async def _gemini_bank_json(self, *, image_bytes: bytes, mime_type: str) -> AiJsonResult:
        if not self.settings.gemini_api_key:
            return AiJsonResult(provider=AiProvider.gemini, data=None, error="missing_gemini_api_key")

        prompt = (
            "Read this image only if it is a bank app, card app, wallet app, or account statement "
            "screenshot showing transactions. If it is a grocery receipt or unrelated image, return "
            "{\"transactions\": []}. Return only JSON with key transactions. transactions must be an "
            "array of objects with keys description, merchant, amount, occurred_on as YYYY-MM-DD or null, "
            "transaction_type as expense or income, category. Categories should be one of: Food, Eat Out, "
            "Commute, Sport, Entertainment, Health, Beauty, House Chemicals, Taxes, Utilities, Income, Other."
        )
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.settings.gemini_model}:generateContent"
        )
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": b64encode(image_bytes).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        }
        headers = {"x-goog-api-key": self.settings.gemini_api_key}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
            candidate = response.json()["candidates"][0]
            text = candidate["content"]["parts"][0]["text"]
            return AiJsonResult(provider=AiProvider.gemini, data=json.loads(text))
        except Exception as exc:
            logger.info("Gemini bank screenshot extraction failed: %s", exc)
            return AiJsonResult(provider=AiProvider.gemini, data=None, error=str(exc))
