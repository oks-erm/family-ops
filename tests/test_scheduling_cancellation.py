import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request

from app.routes.scheduling import (
    _remove_booking_calendar_event,
    student_cancel_lesson,
)
from app.services.calendar_service import CalendarSyncError


def _student_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/",
            "query_string": b"",
            "headers": [(b"host", b"lessons.example.com")],
            "server": ("lessons.example.com", 443),
            "session": {
                "student_google_email": "student@example.com",
                "student_google_name": "Student",
            },
        }
    )


class BookingCalendarCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_google_deletion_clears_stored_event_reference(self) -> None:
        household_id = uuid4()
        session = AsyncMock()
        booking = SimpleNamespace(
            external_calendar_id="tutor@example.com",
            external_event_id="event-123",
        )

        with patch("app.routes.scheduling.CalendarService") as service_type:
            service_type.return_value.delete_google_event_by_id = AsyncMock()
            await _remove_booking_calendar_event(
                session=session,
                household_id=household_id,
                booking=booking,
            )

        service_type.return_value.delete_google_event_by_id.assert_awaited_once_with(
            household_id=household_id,
            calendar_id="tutor@example.com",
            event_id="event-123",
        )
        self.assertIsNone(booking.external_event_id)

    async def test_failed_google_deletion_keeps_event_reference_for_retry(self) -> None:
        session = AsyncMock()
        booking = SimpleNamespace(
            external_calendar_id="tutor@example.com",
            external_event_id="event-123",
        )

        with patch("app.routes.scheduling.CalendarService") as service_type:
            service_type.return_value.delete_google_event_by_id = AsyncMock(
                side_effect=CalendarSyncError("Google event is still active.")
            )
            with self.assertRaises(HTTPException) as raised:
                await _remove_booking_calendar_event(
                    session=session,
                    household_id=uuid4(),
                    booking=booking,
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(booking.external_event_id, "event-123")

    async def test_cancelled_booking_retries_calendar_cleanup(self) -> None:
        profile_id = uuid4()
        booking_id = uuid4()
        profile = SimpleNamespace(id=profile_id, household_id=uuid4())
        booking = SimpleNamespace(
            id=booking_id,
            profile_id=profile_id,
            student_email="student@example.com",
            status="cancelled",
            external_calendar_id="tutor@example.com",
            external_event_id="event-123",
        )
        session = AsyncMock()
        session.get.return_value = booking

        @asynccontextmanager
        async def session_factory():
            yield session

        with (
            patch("app.routes.scheduling.async_session_factory", session_factory),
            patch("app.routes.scheduling.SchedulingRepository") as repository_type,
            patch(
                "app.routes.scheduling._remove_booking_calendar_event",
                new=AsyncMock(),
            ) as remove_event,
        ):
            repository_type.return_value.profile_by_slug = AsyncMock(return_value=profile)
            result = await student_cancel_lesson(
                _student_request(), "oksana-erm", booking_id
            )

        self.assertTrue(result["cancelled"])
        remove_event.assert_awaited_once_with(
            session=session,
            household_id=profile.household_id,
            booking=booking,
        )
        session.commit.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
