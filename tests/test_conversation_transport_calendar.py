import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.bot.handlers.messages import handle_text_message
from app.config import Settings
from app.db.models import CalendarProvider
from app.services.calendar_service import CalendarEventMatchError, CalendarService


class TelegramConversationTests(unittest.IsolatedAsyncioTestCase):
    def message(self, kind="private"):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=123, first_name="Test", last_name=None, username=None),
            text="Hello",
            message_id=42,
            chat=SimpleNamespace(id=123, type=kind),
            answer=AsyncMock(),
        )

    async def test_group_cannot_overwrite_private_reminder_destination(self):
        message = self.message("group")
        with (
            patch(
                "app.bot.handlers.messages.get_settings",
                return_value=Settings(_env_file=None, ASSISTANT_V2_ENABLED=True),
            ),
            patch("app.bot.handlers.messages.async_session_factory") as factory,
        ):
            await handle_text_message(message)
        factory.assert_not_called()
        message.answer.assert_awaited_once()

    async def test_model_reply_is_sent_as_plain_text(self):
        message = self.message()

        @asynccontextmanager
        async def factory():
            yield AsyncMock()

        with (
            patch(
                "app.bot.handlers.messages.get_settings",
                return_value=Settings(_env_file=None, ASSISTANT_V2_ENABLED=True),
            ),
            patch("app.bot.handlers.messages.async_session_factory", factory),
            patch(
                "app.bot.handlers.messages.UserRepository.upsert_telegram_user",
                AsyncMock(return_value=SimpleNamespace(id=uuid4())),
            ),
            patch(
                "app.bot.handlers.messages.ConversationService.handle",
                AsyncMock(return_value="<b>Plain content</b>"),
            ),
        ):
            await handle_text_message(message)
        message.answer.assert_awaited_once_with("<b>Plain content</b>", parse_mode=None)


class HouseholdCalendarEditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.household_id = uuid4()
        self.session = AsyncMock()
        self.connection = SimpleNamespace(
            household_id=self.household_id,
            scopes=["https://www.googleapis.com/auth/calendar.events"],
        )
        self.session.get.return_value = self.connection
        self.service = CalendarService(self.session)
        self.service._google_access_token = AsyncMock(return_value="synthetic-token")
        self.service.repository.delete_cached_event = AsyncMock()
        self.event = SimpleNamespace(
            household_id=self.household_id,
            source_type=CalendarProvider.google,
            source_id=uuid4(),
            external_event_id="test-calendar:event-1",
            raw_event={"_calendar_id": "test-calendar", "etag": '"original"'},
        )
        self.client = AsyncMock()
        self.client.get.return_value = SimpleNamespace(
            status_code=200, json=lambda: {"etag": '"original"'}, raise_for_status=lambda: None
        )
        self.client.patch.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "id": "event-1",
                "summary": "Updated",
                "etag": '"updated"',
                "start": {"dateTime": "2026-10-01T10:00:00Z"},
                "end": {"dateTime": "2026-10-01T11:00:00Z"},
            },
            raise_for_status=lambda: None,
        )
        self.client.delete.return_value = SimpleNamespace(
            status_code=204, raise_for_status=lambda: None
        )
        self.context = AsyncMock()
        self.context.__aenter__.return_value = self.client

    async def test_stale_calendar_confirmation_cannot_write(self):
        self.client.get.return_value.json = lambda: {"etag": '"changed-elsewhere"'}
        with patch("app.services.calendar_service.httpx.AsyncClient", return_value=self.context):
            with self.assertRaises(CalendarEventMatchError):
                await self.service.change_household_event(
                    event=self.event,
                    household_id=self.household_id,
                    action="update",
                    title="Updated",
                )
        self.client.patch.assert_not_awaited()
        self.client.delete.assert_not_awaited()

    async def test_exact_calendar_edit_sends_version_guard(self):
        with patch("app.services.calendar_service.httpx.AsyncClient", return_value=self.context):
            await self.service.change_household_event(
                event=self.event, household_id=self.household_id, action="update", title="Updated"
            )
        self.assertEqual(self.client.patch.call_args.kwargs["headers"]["If-Match"], '"original"')
        self.assertEqual(self.client.patch.call_args.kwargs["params"], {"sendUpdates": "none"})
        self.assertEqual(self.event.title, "Updated")

    async def test_calendar_edit_cannot_target_another_household(self):
        with self.assertRaises(CalendarEventMatchError):
            await self.service.change_household_event(
                event=self.event, household_id=uuid4(), action="delete"
            )
        self.service._google_access_token.assert_not_awaited()
