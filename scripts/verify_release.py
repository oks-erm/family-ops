"""Read-only deployed-container checks. No messages, household reads, or model calls."""

import asyncio
import json
import sys
from pathlib import Path

import httpx
from aiogram.types import Update
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.db.session import async_session_factory
from app.runtime import telegram_update_payload


async def verify():
    # Pure synthetic transport check: never enqueue, write records, or send a message.
    update = Update.model_validate({
        "update_id": 1,
        "message": {
            "message_id": 1, "date": 0,
            "from": {"id": 1, "is_bot": False, "first_name": "Synthetic"},
            "chat": {"id": 1, "type": "private"}, "text": "Shopping list",
        },
    })
    payload = telegram_update_payload(update)
    assert payload["message"]["from"]["id"] == 1, "Telegram sender serialization failed"
    assert "from_user" not in payload["message"], "Python field names leaked into inbox"
    settings = get_settings()
    assert settings.assistant_v2_enabled, "Conversation engine is disabled"
    assert settings.assistant_model == "gpt-6-luna", "Unexpected everyday model"
    assert settings.assistant_reasoning_model == "gpt-6.1-sol", "Unexpected fallback model"
    assert settings.openai_api_key, "Missing model credentials"
    assert settings.telegram_bot_token, "Missing Telegram configuration"
    async with async_session_factory() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "202609300002", "Unexpected database revision"
        for name in (
            "assistant_conversations",
            "assistant_turns",
            "assistant_actions",
            "assistant_budgets",
            "assistant_model_calls",
            "household_calendar_selections",
            "assistant_inbox",
            "assistant_outbox",
            "assistant_household_policies",
            "runtime_leases",
            "transport_cursors",
        ):
            assert await session.scalar(text("SELECT to_regclass(:name)"), {"name": name}), name
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        for base in dict.fromkeys(["http://127.0.0.1:8000", settings.public_base_url.rstrip("/")]):
            response = await client.get(base + "/health")
            assert response.status_code == 200 and response.json() == {"status": "ok"}
        for path in ("/schedule/manage", "/book/example", "/api/scheduling/manage"):
            response = await client.get("http://127.0.0.1:8000" + path)
            assert response.status_code == 404, "Lesson route exposed by household app"
        response = await client.get("http://127.0.0.1:8000/api/dashboard")
        assert response.status_code == 401, "Dashboard authentication smoke failed"
    print(
        json.dumps(
            {
                "release_checks": "passed",
                "migration": revision,
                "engine_enabled": True,
                "models": [settings.assistant_model, settings.assistant_reasoning_model],
                "internal_and_public_health": "passed",
                "household_lesson_routes": "absent",
                "dashboard_authentication": "passed",
                "telegram_serialization": "passed",
            }
        )
    )


if __name__ == "__main__":
    try:
        asyncio.run(verify())
    except Exception as exc:
        # Keep provider URLs, tokens, and database connection details out of CI logs.
        print(f"Release verification failed ({type(exc).__name__}).", file=sys.stderr)
        sys.exit(1)
