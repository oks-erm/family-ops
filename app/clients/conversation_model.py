"""Responses API adapter with bounded output, no automatic retries, and no payload logging."""

from dataclasses import dataclass
from typing import Protocol

import httpx

from app.schemas.conversation import ConversationAnswer, answer_format


class ModelUnavailable(RuntimeError):
    pass


class InvalidModelResponse(RuntimeError):
    pass


@dataclass
class ModelReply:
    output: list[dict]
    usage: dict
    status: str
    answer: ConversationAnswer | None = None

    @property
    def text(self):
        if self.answer is not None:
            return self.answer.reply
        return "\n".join(
            part["text"]
            for item in self.output
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ).strip()

    @property
    def calls(self):
        return [item for item in self.output if item.get("type") == "function_call"]


class ConversationModel(Protocol):
    async def respond(
        self, *, model: str, instructions: str, inputs: list[dict], tools: list[dict]
    ) -> ModelReply: ...


class OpenAIConversationModel:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    async def respond(self, *, model, instructions, inputs, tools):
        if not self.settings.openai_api_key:
            raise ModelUnavailable("Model credentials are not configured")
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.assistant_timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    "https://api.openai.com/v1/responses",
                    headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                    json={
                        "model": model,
                        "instructions": instructions,
                        "input": inputs,
                        "tools": tools,
                        "store": False,
                        "parallel_tool_calls": False,
                        "max_output_tokens": self.settings.assistant_max_output_tokens,
                        "text": {"format": answer_format()},
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            # Never include URLs, headers, response bodies, or payloads in public errors.
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            detail = f"HTTP {status}" if status else type(exc).__name__
            raise ModelUnavailable(f"The conversation model is unavailable ({detail})") from exc
        try:
            data = response.json()
            if not isinstance(data.get("output"), list) or not isinstance(data.get("usage"), dict):
                raise ValueError("Missing output or usage")
            usage = data["usage"]
            for key in ("input_tokens", "output_tokens"):
                if type(usage.get(key)) is not int or usage[key] < 0:
                    raise ValueError("Invalid usage accounting")
            details = usage.get("input_tokens_details", {})
            if not isinstance(details, dict):
                raise ValueError("Invalid input usage details")
            for key in ("cached_tokens", "cache_write_tokens"):
                value = details.get(key, 0)
                if type(value) is not int or not 0 <= value <= usage["input_tokens"]:
                    raise ValueError("Invalid cache usage accounting")
            if (
                sum(details.get(key, 0) for key in ("cached_tokens", "cache_write_tokens"))
                > usage["input_tokens"]
            ):
                raise ValueError("Cache usage exceeds total input")
            if not all(isinstance(item, dict) for item in data["output"]):
                raise ValueError("Invalid output items")
            reply = ModelReply(
                output=data["output"], usage=data["usage"], status=data.get("status", "incomplete")
            )
            if reply.status == "completed" and not reply.calls and reply.text:
                reply.answer = ConversationAnswer.model_validate_json(reply.text)
            return reply
        except (ValueError, TypeError, KeyError) as exc:
            raise InvalidModelResponse("The model returned an invalid response") from exc
