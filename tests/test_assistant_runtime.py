import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import GetUpdates
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.runtime import ingress


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingress_does_not_acknowledge_unpersisted_update(self):
        queue = SimpleNamespace(
            offset=AsyncMock(return_value=123),
            ingest=AsyncMock(side_effect=RuntimeError("Synthetic DB failure")),
        )
        update = SimpleNamespace(model_dump=lambda **kw: {"update_id": 123})
        bot = SimpleNamespace(get_updates=AsyncMock(return_value=[update]))
        dispatcher = SimpleNamespace(resolve_used_update_types=lambda: ["message"])
        with patch("app.runtime.create_dispatcher", return_value=dispatcher):
            with self.assertRaises(RuntimeError):
                await ingress(queue, bot, Settings(_env_file=None), {"at": 0})
        bot.get_updates.assert_awaited_once()
        self.assertEqual(bot.get_updates.call_args.kwargs["offset"], 123)

    async def test_invalid_polling_credentials_fail_instead_of_reporting_healthy(self):
        queue = SimpleNamespace(offset=AsyncMock(return_value=0), ingest=AsyncMock())
        bot = SimpleNamespace(
            get_updates=AsyncMock(
                side_effect=TelegramUnauthorizedError(
                    method=GetUpdates(), message="synthetic unauthorized"
                )
            )
        )
        progress = {"at": 0}
        with patch(
            "app.runtime.create_dispatcher",
            return_value=SimpleNamespace(resolve_used_update_types=lambda: ["message"]),
        ):
            with self.assertRaisesRegex(RuntimeError, "configuration was rejected"):
                await ingress(queue, bot, Settings(_env_file=None), progress)
        self.assertEqual(progress["at"], 0)
        queue.ingest.assert_not_awaited()

    def test_web_lifespan_never_starts_telegram_or_scheduled_jobs(self):
        with (
            patch("app.bot.main.create_bot") as bot,
            patch("app.services.scheduler_service.SchedulerService.start") as scheduler,
        ):
            with TestClient(app) as client:
                self.assertEqual(client.get("/health").json(), {"status": "ok"})
                self.assertEqual(client.get("/schedule/manage").status_code, 404)
                self.assertEqual(client.get("/api/dashboard").status_code, 401)
        bot.assert_not_called()
        scheduler.assert_not_called()
