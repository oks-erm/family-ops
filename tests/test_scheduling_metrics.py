import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.services.scheduling_metrics import monthly_scheduling_metrics


class MonthlySchedulingMetricsTests(unittest.TestCase):
    def test_uses_registered_payment_value_for_completed_and_scheduled_lessons(self) -> None:
        payment_id = uuid4()
        completed_id = uuid4()
        scheduled_id = uuid4()
        cancelled_id = uuid4()
        bookings = [
            SimpleNamespace(
                id=completed_id,
                status="confirmed",
                starts_at=datetime(2026, 7, 3, 9, tzinfo=UTC),
                ends_at=datetime(2026, 7, 3, 10, tzinfo=UTC),
            ),
            SimpleNamespace(
                id=scheduled_id,
                status="confirmed",
                starts_at=datetime(2026, 7, 29, 9, tzinfo=UTC),
                ends_at=datetime(2026, 7, 29, 10, tzinfo=UTC),
            ),
            SimpleNamespace(
                id=cancelled_id,
                status="cancelled",
                starts_at=datetime(2026, 7, 10, 9, tzinfo=UTC),
                ends_at=datetime(2026, 7, 10, 10, tzinfo=UTC),
            ),
        ]
        payment = SimpleNamespace(
            id=payment_id,
            lessons_purchased=8,
            amount_cents=22400,
        )
        allocations = [
            SimpleNamespace(payment_id=payment_id, booking_id=completed_id),
            SimpleNamespace(payment_id=payment_id, booking_id=scheduled_id),
            SimpleNamespace(payment_id=payment_id, booking_id=cancelled_id),
        ]

        metrics = monthly_scheduling_metrics(
            bookings=bookings,
            payments=[payment],
            allocations=allocations,
            timezone="Europe/Lisbon",
            now=datetime(2026, 7, 27, 12, tzinfo=UTC),
        )

        self.assertEqual(metrics.completed_lessons, 1)
        self.assertEqual(metrics.total_lessons, 2)
        self.assertEqual(metrics.earned_cents, 2800)
        self.assertEqual(metrics.projected_cents, 5600)

    def test_unregistered_or_unallocated_lessons_have_no_recognized_income(self) -> None:
        booking = SimpleNamespace(
            id=uuid4(),
            status="confirmed",
            starts_at=datetime(2026, 7, 3, 9, tzinfo=UTC),
            ends_at=datetime(2026, 7, 3, 10, tzinfo=UTC),
        )

        metrics = monthly_scheduling_metrics(
            bookings=[booking],
            payments=[],
            allocations=[],
            timezone="UTC",
            now=datetime(2026, 7, 27, 12, tzinfo=UTC),
        )

        self.assertEqual(metrics.completed_lessons, 1)
        self.assertEqual(metrics.total_lessons, 1)
        self.assertEqual(metrics.earned_cents, 0)
        self.assertEqual(metrics.projected_cents, 0)
