import unittest
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.db.models import CalendarConnection, CalendarProvider
from app.services.scheduling_rules import (
    BusyPeriod,
    SchedulingValidationError,
    generate_slots,
    normalize_slug,
    periods_overlap,
    validate_timezone,
)
from app.services.scheduling_service import SchedulingService
from app.utils.urls import UnsafeExternalURLError, validate_public_https_url


class SchedulingRulesTests(unittest.TestCase):
    def test_periods_touching_at_boundary_do_not_overlap(self) -> None:
        first_start = datetime(2026, 7, 27, 9, tzinfo=UTC)
        first_end = datetime(2026, 7, 27, 10, tzinfo=UTC)

        self.assertFalse(
            periods_overlap(
                first_start,
                first_end,
                first_end,
                datetime(2026, 7, 27, 11, tzinfo=UTC),
            )
        )

    def test_generates_interval_slots_that_fit_inside_window(self) -> None:
        slots = generate_slots(
            day=date(2026, 7, 27),  # Monday
            timezone="Europe/Lisbon",
            rules=[(0, time(9), time(11))],
            duration_minutes=60,
            interval_minutes=30,
            buffer_before_minutes=0,
            buffer_after_minutes=0,
            earliest_start=datetime(2026, 7, 27, 8, tzinfo=UTC),
            latest_start=datetime(2026, 7, 27, 18, tzinfo=UTC),
            busy_periods=[],
        )

        self.assertEqual([slot.strftime("%H:%M") for slot in slots], ["09:00", "09:30", "10:00"])

    def test_buffers_block_adjacent_slot(self) -> None:
        slots = generate_slots(
            day=date(2026, 7, 27),
            timezone="UTC",
            rules=[(0, time(9), time(12))],
            duration_minutes=60,
            interval_minutes=60,
            buffer_before_minutes=0,
            buffer_after_minutes=15,
            earliest_start=datetime(2026, 7, 27, 8, tzinfo=UTC),
            latest_start=datetime(2026, 7, 27, 18, tzinfo=UTC),
            busy_periods=[
                BusyPeriod(
                    datetime(2026, 7, 27, 10, tzinfo=UTC),
                    datetime(2026, 7, 27, 11, tzinfo=UTC),
                )
            ],
        )

        self.assertEqual([slot.strftime("%H:%M") for slot in slots], ["11:00"])

    def test_lessons_can_be_back_to_back_without_commute_buffer(self) -> None:
        slots = generate_slots(
            day=date(2026, 7, 27),
            timezone="UTC",
            rules=[(0, time(9), time(12))],
            duration_minutes=60,
            interval_minutes=60,
            buffer_before_minutes=60,
            buffer_after_minutes=60,
            earliest_start=datetime(2026, 7, 27, 8, tzinfo=UTC),
            latest_start=datetime(2026, 7, 27, 18, tzinfo=UTC),
            busy_periods=[
                BusyPeriod(
                    datetime(2026, 7, 27, 10, tzinfo=UTC),
                    datetime(2026, 7, 27, 11, tzinfo=UTC),
                    requires_buffer=False,
                )
            ],
        )

        self.assertEqual([slot.strftime("%H:%M") for slot in slots], ["09:00", "11:00"])

    def test_commute_buffer_applies_on_both_sides_of_non_lesson_event(self) -> None:
        slots = generate_slots(
            day=date(2026, 7, 27),
            timezone="UTC",
            rules=[(0, time(9), time(13))],
            duration_minutes=60,
            interval_minutes=60,
            buffer_before_minutes=60,
            buffer_after_minutes=60,
            earliest_start=datetime(2026, 7, 27, 8, tzinfo=UTC),
            latest_start=datetime(2026, 7, 27, 18, tzinfo=UTC),
            busy_periods=[
                BusyPeriod(
                    datetime(2026, 7, 27, 10, tzinfo=UTC),
                    datetime(2026, 7, 27, 11, tzinfo=UTC),
                )
            ],
        )

        self.assertEqual([slot.strftime("%H:%M") for slot in slots], ["12:00"])

    def test_only_rules_for_requested_weekday_are_used(self) -> None:
        slots = generate_slots(
            day=date(2026, 7, 27),
            timezone="UTC",
            rules=[(1, time(9), time(11))],
            duration_minutes=60,
            interval_minutes=30,
            buffer_before_minutes=0,
            buffer_after_minutes=0,
            earliest_start=datetime(2026, 7, 27, 8, tzinfo=UTC),
            latest_start=datetime(2026, 7, 27, 18, tzinfo=UTC),
            busy_periods=[],
        )

        self.assertEqual(slots, [])

    def test_slug_and_timezone_validation(self) -> None:
        self.assertEqual(normalize_slug(" Oksana's English Lessons "), "oksana-s-english-lessons")
        self.assertEqual(validate_timezone("Europe/Lisbon"), "Europe/Lisbon")
        with self.assertRaises(SchedulingValidationError):
            validate_timezone("Moon/Sea_of_Tranquility")


class ExternalURLSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_http_and_local_addresses(self) -> None:
        for url in (
            "http://calendar.example.com/feed.ics",
            "https://localhost/feed.ics",
            "https://127.0.0.1/feed.ics",
            "https://169.254.169.254/latest/meta-data",
        ):
            with self.subTest(url=url), self.assertRaises(UnsafeExternalURLError):
                await validate_public_https_url(url)


class BookingCalendarRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_refreshes_only_connections_with_enabled_calendars(self) -> None:
        household_id = uuid4()
        selected_connection_id = uuid4()
        stale_connection_id = uuid4()
        session = AsyncMock()
        session.get.return_value = SimpleNamespace(
            id=selected_connection_id,
            household_id=household_id,
            provider=CalendarProvider.google,
        )
        service = SchedulingService(session)
        service.repository.list_calendars = AsyncMock(
            return_value=[
                SimpleNamespace(
                    connection_id=selected_connection_id,
                    include_in_conflicts=True,
                ),
                SimpleNamespace(
                    connection_id=stale_connection_id,
                    include_in_conflicts=False,
                ),
            ]
        )

        with patch("app.services.scheduling_service.CalendarService") as service_type:
            calendar_service = service_type.return_value
            calendar_service.sync_ical_feeds = AsyncMock()
            calendar_service.sync_google_connections = AsyncMock()
            calendar_service.sync_icloud_connections = AsyncMock()

            await service._refresh_booking_calendars(
                profile=SimpleNamespace(id=uuid4(), household_id=household_id)
            )

        session.get.assert_awaited_once_with(CalendarConnection, selected_connection_id)
        calendar_service.sync_google_connections.assert_awaited_once_with(
            household_id=household_id,
            connection_id=selected_connection_id,
        )


class BatchBookingValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_is_limited_to_ten_unique_non_overlapping_times(self) -> None:
        service = SchedulingService(AsyncMock())
        profile = SimpleNamespace()
        lesson_type = SimpleNamespace(duration_minutes=60)
        base = datetime(2026, 7, 27, 9, tzinfo=UTC)

        with self.assertRaisesRegex(SchedulingValidationError, "between 1 and 10"):
            await service.book_many(
                profile=profile,
                lesson_type=lesson_type,
                starts_at=[base.replace(hour=hour) for hour in range(9, 20)],
                student_name="Student",
                student_email="student@example.com",
                student_timezone="UTC",
                notes=None,
            )

        with self.assertRaisesRegex(SchedulingValidationError, "unique"):
            await service.book_many(
                profile=profile,
                lesson_type=lesson_type,
                starts_at=[base, base],
                student_name="Student",
                student_email="student@example.com",
                student_timezone="UTC",
                notes=None,
            )

        with self.assertRaisesRegex(SchedulingValidationError, "cannot overlap"):
            await service.book_many(
                profile=profile,
                lesson_type=lesson_type,
                starts_at=[base, base.replace(minute=30)],
                student_name="Student",
                student_email="student@example.com",
                student_timezone="UTC",
                notes=None,
            )

if __name__ == "__main__":
    unittest.main()
