import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request

from app.routes.scheduling import BookingRequest, public_book
from app.services.scheduling_service import SchedulingService


def _request(*, session: dict[str, object] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/api/public/scheduling/tutor/book",
            "query_string": b"",
            "headers": [(b"host", b"lessons.example.com")],
            "server": ("lessons.example.com", 443),
            "session": session if session is not None else {},
        }
    )


def _payload(*, starts_at: list[datetime] | None = None) -> BookingRequest:
    return BookingRequest(
        lesson_type_id=uuid4(),
        starts_at=starts_at or [datetime(2026, 8, 3, 9, tzinfo=UTC)],
        student_timezone="UTC",
        student_name=" Guest Student ",
        student_email=" GUEST@Example.com ",
    )


class GuestBookingRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_booking_uses_unverified_identity_for_one_time_meet(self) -> None:
        profile = SimpleNamespace(id=uuid4())
        lesson_type = SimpleNamespace(id=uuid4(), is_active=True)
        starts_at = datetime(2026, 8, 3, 9, tzinfo=UTC)
        booking = SimpleNamespace(
            id=uuid4(),
            status="confirmed",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
        )
        session = AsyncMock()

        @asynccontextmanager
        async def session_factory():
            yield session

        with (
            patch("app.routes.scheduling.async_session_factory", session_factory),
            patch("app.routes.scheduling.SchedulingRepository") as repository_type,
            patch("app.routes.scheduling.SchedulingService") as service_type,
        ):
            repository_type.return_value.profile_by_slug = AsyncMock(return_value=profile)
            repository_type.return_value.lesson_type = AsyncMock(return_value=lesson_type)
            service_type.return_value.book_many = AsyncMock(return_value=[booking])

            result = await public_book(_request(), "tutor", _payload())

        self.assertEqual(result["count"], 1)
        service_type.return_value.book_many.assert_awaited_once_with(
            profile=profile,
            lesson_type=lesson_type,
            starts_at=[starts_at],
            student_name="Guest Student",
            student_email="guest@example.com",
            student_timezone="UTC",
            notes=None,
            guest_booking=True,
        )

    async def test_guest_booking_rejects_multiple_times(self) -> None:
        starts_at = [
            datetime(2026, 8, 3, 9, tzinfo=UTC),
            datetime(2026, 8, 4, 9, tzinfo=UTC),
        ]

        with self.assertRaises(HTTPException) as raised:
            await public_book(_request(), "tutor", _payload(starts_at=starts_at))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Sign in with Google", raised.exception.detail)

    async def test_signed_in_booking_ignores_submitted_guest_identity(self) -> None:
        profile = SimpleNamespace(id=uuid4())
        lesson_type = SimpleNamespace(id=uuid4(), is_active=True)
        starts_at = datetime(2026, 8, 3, 9, tzinfo=UTC)
        booking = SimpleNamespace(
            id=uuid4(),
            status="confirmed",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
        )
        session = AsyncMock()

        @asynccontextmanager
        async def session_factory():
            yield session

        with (
            patch("app.routes.scheduling.async_session_factory", session_factory),
            patch("app.routes.scheduling.SchedulingRepository") as repository_type,
            patch("app.routes.scheduling.SchedulingService") as service_type,
        ):
            repository_type.return_value.profile_by_slug = AsyncMock(return_value=profile)
            repository_type.return_value.lesson_type = AsyncMock(return_value=lesson_type)
            service_type.return_value.book_many = AsyncMock(return_value=[booking])

            await public_book(
                _request(
                    session={
                        "student_google_email": "verified@example.com",
                        "student_google_name": "Verified Student",
                    }
                ),
                "tutor",
                _payload(),
            )

        call = service_type.return_value.book_many.await_args.kwargs
        self.assertEqual(call["student_email"], "verified@example.com")
        self.assertEqual(call["student_name"], "Verified Student")
        self.assertFalse(call["guest_booking"])


class GuestBookingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_booking_creates_fresh_meet_without_credit_allocation(self) -> None:
        session = AsyncMock()
        service = SchedulingService(session)
        start = datetime(2026, 8, 3, 9, tzinfo=UTC)
        profile = SimpleNamespace(
            id=uuid4(),
            household_id=uuid4(),
            user_id=uuid4(),
            timezone="UTC",
            booking_calendar_id=None,
        )
        lesson_type = SimpleNamespace(
            id=uuid4(),
            name="English lesson",
            duration_minutes=60,
            location=None,
        )
        calendar_event = SimpleNamespace(
            meeting_url="https://meet.google.com/guest-room",
            conference_data={"conferenceId": "guest-room"},
            external_calendar_id="primary",
            external_event_id="event-123",
        )
        calendar_service = SimpleNamespace(
            create_google_event=AsyncMock(return_value=calendar_event),
            delete_google_event_by_id=AsyncMock(),
        )
        service.repository.recent_booking_count = AsyncMock(return_value=0)
        service.repository.lock_profile = AsyncMock()
        service.repository.student_meeting = AsyncMock()
        service.repository.add_student_meeting = AsyncMock()
        service.repository.add_booking = AsyncMock()
        service.slots = AsyncMock(return_value=[start])
        service._refresh_booking_calendars = AsyncMock(return_value=calendar_service)
        service._allocate_available_credits = AsyncMock()

        bookings = await service.book_many(
            profile=profile,
            lesson_type=lesson_type,
            starts_at=[start],
            student_name="Guest Student",
            student_email="guest@example.com",
            student_timezone="UTC",
            notes=None,
            guest_booking=True,
        )

        self.assertEqual(len(bookings), 1)
        service.repository.student_meeting.assert_not_awaited()
        service.repository.add_student_meeting.assert_not_awaited()
        service._allocate_available_credits.assert_not_awaited()
        event_call = calendar_service.create_google_event.await_args.kwargs
        self.assertIsNone(event_call["conference_data"])
        self.assertTrue(event_call["create_google_meet"])
        self.assertEqual(event_call["attendee_email"], "guest@example.com")


if __name__ == "__main__":
    unittest.main()
