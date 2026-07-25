import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.db.models import CalendarProvider
from app.services.calendar_service import CalendarService


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _Client:
    def __init__(self, event: dict[str, object]) -> None:
        self.event = event
        self.post_calls: list[dict[str, object]] = []
        self.patch_calls: list[dict[str, object]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, url: str, **kwargs) -> _Response:
        self.post_calls.append({"url": url, **kwargs})
        return _Response(self.event)

    async def patch(self, url: str, **kwargs) -> _Response:
        self.patch_calls.append({"url": url, **kwargs})
        return _Response(self.event)


def _google_event() -> dict[str, object]:
    return {
        "id": "event123",
        "summary": "English lesson",
        "start": {"dateTime": "2026-07-27T09:00:00+00:00"},
        "end": {"dateTime": "2026-07-27T10:00:00+00:00"},
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
        "conferenceData": {
            "conferenceId": "abc-defg-hij",
            "conferenceSolution": {"key": {"type": "hangoutsMeet"}},
            "entryPoints": [
                {
                    "entryPointType": "video",
                    "uri": "https://meet.google.com/abc-defg-hij",
                }
            ],
            "createRequest": {
                "requestId": "request-1",
                "status": {"statusCode": "success"},
            },
        },
    }


class GoogleMeetEventTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, client: _Client) -> CalendarService:
        service = CalendarService(AsyncMock())
        service.repository.upsert_event = AsyncMock()
        service._google_write_connection = AsyncMock(
            return_value=SimpleNamespace(id=uuid4())
        )
        service._connection_calendar_id = lambda _connection: "primary"
        service._google_access_token = AsyncMock(return_value="access-token")
        self.client_patch = patch(
            "app.services.calendar_service.httpx.AsyncClient",
            return_value=client,
        )
        return service

    async def test_new_student_meet_is_created_before_invitation_is_sent(self) -> None:
        client = _Client(_google_event())
        service = self._service(client)

        with self.client_patch:
            result = await service.create_google_event(
                household_id=uuid4(),
                user_id=uuid4(),
                title="English lesson",
                starts_at=datetime(2026, 7, 27, 9, tzinfo=UTC),
                ends_at=datetime(2026, 7, 27, 10, tzinfo=UTC),
                timezone="UTC",
                attendee_email="Student@Example.com",
                create_google_meet=True,
            )

        self.assertNotIn("attendees", client.post_calls[0]["json"])
        self.assertEqual(client.post_calls[0]["params"], {"conferenceDataVersion": 1})
        self.assertEqual(
            client.patch_calls[0]["json"],
            {"attendees": [{"email": "student@example.com"}]},
        )
        self.assertEqual(
            client.patch_calls[0]["params"],
            {"conferenceDataVersion": 1, "sendUpdates": "all"},
        )
        self.assertEqual(result.meeting_url, "https://meet.google.com/abc-defg-hij")
        self.assertEqual(result.conference_data, _google_event()["conferenceData"])

    async def test_existing_student_meet_is_copied_to_new_lesson(self) -> None:
        event = _google_event()
        conference_data = event["conferenceData"]
        client = _Client(event)
        service = self._service(client)

        with self.client_patch:
            result = await service.create_google_event(
                household_id=uuid4(),
                user_id=uuid4(),
                title="English lesson",
                starts_at=datetime(2026, 7, 27, 9, tzinfo=UTC),
                ends_at=datetime(2026, 7, 27, 10, tzinfo=UTC),
                timezone="UTC",
                attendee_email="student@example.com",
                conference_data=conference_data,
            )

        self.assertEqual(client.post_calls[0]["json"]["conferenceData"], conference_data)
        self.assertEqual(
            client.post_calls[0]["json"]["attendees"],
            [{"email": "student@example.com"}],
        )
        self.assertEqual(
            client.post_calls[0]["params"],
            {"conferenceDataVersion": 1, "sendUpdates": "all"},
        )
        self.assertEqual(client.patch_calls, [])
        self.assertEqual(result.meeting_url, "https://meet.google.com/abc-defg-hij")


class GoogleCalendarAccountIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_can_target_one_google_account(self) -> None:
        first = SimpleNamespace(id=uuid4())
        second = SimpleNamespace(id=uuid4())
        service = CalendarService(AsyncMock())
        service.repository.list_google_connections = AsyncMock(
            return_value=[first, second]
        )
        service._google_access_token = AsyncMock(return_value=None)

        await service.sync_google_connections(connection_id=second.id)

        service._google_access_token.assert_awaited_once()
        self.assertIs(service._google_access_token.await_args.kwargs["connection"], second)

    async def test_default_write_uses_a_discovered_writable_account(self) -> None:
        writable = SimpleNamespace(
            id=uuid4(),
            provider=CalendarProvider.google,
            scopes=["https://www.googleapis.com/auth/calendar.events"],
        )
        result = MagicMock()
        result.scalar_one_or_none.return_value = writable
        session = AsyncMock()
        session.execute.return_value = result
        service = CalendarService(session)
        service.repository.first_google_connection = AsyncMock()

        connection = await service._google_write_connection(household_id=uuid4())

        self.assertIs(connection, writable)
        service.repository.first_google_connection.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
