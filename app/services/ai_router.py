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
            "Keys: intent, confidence, reply, item, title, date_ref, store_name, target, "
            "source, start_time, end_time, kind, duration_minutes.\n"
            "Allowed intents: smalltalk, capability_question, add_shopping_item, going_to_store, "
            "shopping_summary, task_created, planning_query, planning_note, fixed_event, "
            "work_hours_update, sleep_window_update, mark_task_done, mark_shopping_purchased, "
            "remove_item, move_item, finance_transaction, expense_query, clarify, unknown.\n"
            "Rules:\n"
            "- Messages starting with need or buy are shopping unless they are clearly impossible "
            "as products; if impossible, use clarify instead of task_created.\n"
            "- Use add_shopping_item only for concrete products to buy, never activities.\n"
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
            "- Use move_item when moving between shopping/task/plan or rescheduling to today/tomorrow.\n"
            "- For smalltalk/capability_question/clarify, include a short natural reply.\n"
            "- If unsure whether shopping or task, intent clarify and ask a concise question in reply.\n"
            "- confidence is 0..1. date_ref should be today, tonight, tomorrow, week, or empty.\n"
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
