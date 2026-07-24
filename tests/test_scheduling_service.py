import unittest
from datetime import UTC, date, datetime, time

from app.services.scheduling_rules import (
    BusyPeriod,
    SchedulingValidationError,
    generate_slots,
    normalize_slug,
    periods_overlap,
    validate_timezone,
)
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


if __name__ == "__main__":
    unittest.main()
