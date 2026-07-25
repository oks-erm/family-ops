import unittest
from datetime import UTC, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.db.models import CalendarProvider
from app.services.calendar_service import (
    CalDAVProtocolError,
    CalendarService,
    ICloudAuthenticationError,
)
from app.services.credential_cipher import CredentialCipher, CredentialDecryptionError
from app.services.scheduling_service import SchedulingService


class _Response:
    def __init__(
        self,
        *,
        text: str = "",
        status_code: int = 207,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}


class _Client:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    async def request(self, method: str, url: str, **kwargs) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


def _multistatus(properties: str, *, href: str = "/") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{properties}</d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        "</d:multistatus>"
    )


class CredentialCipherTests(unittest.TestCase):
    def test_encrypts_and_decrypts_without_storing_plaintext(self) -> None:
        cipher = CredentialCipher("test-server-secret")

        encrypted = cipher.encrypt("abcd-efgh-ijkl-mnop")

        self.assertTrue(encrypted.startswith("fernet-v1:"))
        self.assertNotIn("abcd-efgh", encrypted)
        self.assertEqual(cipher.decrypt(encrypted), "abcd-efgh-ijkl-mnop")

    def test_wrong_server_secret_cannot_decrypt(self) -> None:
        encrypted = CredentialCipher("first-secret").encrypt("app-password")

        with self.assertRaises(CredentialDecryptionError):
            CredentialCipher("second-secret").decrypt(encrypted)

    def test_refuses_unencrypted_stored_credentials(self) -> None:
        with self.assertRaises(CredentialDecryptionError):
            CredentialCipher("test-secret").decrypt("plain-password")


class ICloudCalDAVTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.session = MagicMock()
        self.session.execute = AsyncMock()
        self.session.commit = AsyncMock()
        self.service = CalendarService(self.session)
        self.connection = SimpleNamespace(
            id=uuid4(),
            user_id=uuid4(),
            household_id=uuid4(),
            account_email="tutor@icloud.com",
            access_token="encrypted",
            external_account_id=None,
        )

    def test_rejects_caldav_urls_outside_icloud(self) -> None:
        with self.assertRaises(CalDAVProtocolError):
            self.service._icloud_url(
                "https://caldav.icloud.com/", "https://attacker.example/calendar"
            )

    async def test_caldav_request_rejects_bad_credentials(self) -> None:
        client = _Client([_Response(status_code=401)])

        with self.assertRaises(ICloudAuthenticationError):
            await self.service._caldav_request(
                client=client,
                connection=self.connection,
                password="wrong-password",
                method="PROPFIND",
                url="https://caldav.icloud.com/",
                depth="0",
                body="<xml/>",
            )

    async def test_discovers_read_only_event_calendars(self) -> None:
        principal = _Response(
            text=_multistatus(
                "<d:current-user-principal><d:href>/123/principal/</d:href>"
                "</d:current-user-principal>"
            )
        )
        home = _Response(
            text=_multistatus(
                "<c:calendar-home-set><d:href>/123/calendars/</d:href>"
                "</c:calendar-home-set>"
            )
        )
        calendars = _Response(
            text=_multistatus(
                "<d:displayname>WORK</d:displayname>"
                "<d:resourcetype><c:calendar/></d:resourcetype>"
                '<c:supported-calendar-component-set><c:comp name="VEVENT"/>'
                "</c:supported-calendar-component-set>",
                href="/123/calendars/work/",
            )
        )
        self.service._icloud_password = lambda _connection: "app-password"
        self.service._caldav_request = AsyncMock(
            side_effect=[principal, home, calendars]
        )
        new_calendar_result = SimpleNamespace(scalar_one_or_none=lambda: None)
        existing_calendars_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [])
        )
        self.session.execute = AsyncMock(
            side_effect=[new_calendar_result, existing_calendars_result]
        )
        profile = SimpleNamespace(id=uuid4())

        count = await self.service.discover_icloud_calendars(
            profile=profile, connection=self.connection
        )

        self.assertEqual(count, 1)
        calendar = self.session.add.call_args.args[0]
        self.assertEqual(calendar.name, "WORK")
        self.assertTrue(calendar.include_in_conflicts)
        self.assertFalse(calendar.can_write)
        self.assertEqual(
            calendar.external_calendar_id,
            "https://caldav.icloud.com/123/calendars/work/",
        )

    async def test_syncs_private_icloud_events_into_conflict_cache(self) -> None:
        calendar = SimpleNamespace(
            external_calendar_id="https://p01-caldav.icloud.com/123/calendars/work/"
        )
        calendar_data = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-1
DTSTART:20260727T090000Z
DTEND:20260727T100000Z
SUMMARY:Private appointment
END:VEVENT
END:VCALENDAR
"""
        response = _Response(
            text=_multistatus(
                f"<d:getetag>etag-1</d:getetag><c:calendar-data><![CDATA[{calendar_data}]]>"
                "</c:calendar-data>",
                href="/123/calendars/work/event-1.ics",
            )
        )
        self.service._caldav_request = AsyncMock(return_value=response)
        self.service.repository.upsert_event = AsyncMock()
        self.service.repository.remove_missing_calendar_events = AsyncMock()

        count = await self.service._sync_icloud_calendar(
            client=AsyncMock(),
            connection=self.connection,
            password="app-password",
            calendar=calendar,
            range_start=datetime(2026, 7, 25, tzinfo=UTC),
            range_end=datetime(2026, 8, 10, tzinfo=UTC),
        )

        self.assertEqual(count, 1)
        call = self.service.repository.upsert_event.call_args.kwargs
        self.assertEqual(call["source_type"], CalendarProvider.icloud)
        self.assertEqual(call["title"], "Private appointment")
        self.assertEqual(call["raw_event"]["_calendar_id"], calendar.external_calendar_id)
        self.service.repository.remove_missing_calendar_events.assert_awaited_once()

    async def test_selected_icloud_calendar_blocks_lesson_slots(self) -> None:
        connection_id = uuid4()
        calendar = SimpleNamespace(
            connection_id=connection_id,
            external_calendar_id="https://p01-caldav.icloud.com/calendars/work/",
            include_in_conflicts=True,
        )
        event = SimpleNamespace(
            source_type=CalendarProvider.icloud,
            source_id=connection_id,
            external_event_id="event-1",
            starts_at=datetime(2026, 7, 27, 9, tzinfo=UTC),
            ends_at=datetime(2026, 7, 27, 10, tzinfo=UTC),
            raw_event={"_calendar_id": calendar.external_calendar_id},
        )
        profile = SimpleNamespace(
            id=uuid4(),
            household_id=uuid4(),
            timezone="UTC",
            minimum_notice_minutes=0,
            booking_window_days=40,
            buffer_before_minutes=0,
            buffer_after_minutes=0,
            slot_interval_minutes=60,
        )
        lesson_type = SimpleNamespace(duration_minutes=60)
        rule = SimpleNamespace(weekday=0, starts_at=time(9), ends_at=time(11))
        scheduling = SchedulingService(MagicMock())
        scheduling.repository.list_calendars = AsyncMock(return_value=[calendar])
        scheduling.repository.busy_events = AsyncMock(return_value=[event])
        scheduling.repository.bookings_between = AsyncMock(return_value=[])
        scheduling.repository.list_rules = AsyncMock(return_value=[rule])

        slots = await scheduling.slots(
            profile=profile,
            lesson_type=lesson_type,
            start_day=datetime(2026, 7, 27).date(),
            end_day=datetime(2026, 7, 27).date(),
            now=datetime(2026, 7, 27, tzinfo=UTC),
        )

        self.assertEqual([slot.hour for slot in slots], [10])

        calendar.include_in_conflicts = False
        slots = await scheduling.slots(
            profile=profile,
            lesson_type=lesson_type,
            start_day=datetime(2026, 7, 27).date(),
            end_day=datetime(2026, 7, 27).date(),
            now=datetime(2026, 7, 27, tzinfo=UTC),
        )
        self.assertEqual([slot.hour for slot in slots], [9, 10])


if __name__ == "__main__":
    unittest.main()
