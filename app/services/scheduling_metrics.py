from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from app.db.models import LessonBooking, LessonPaymentAllocation, StudentPayment


@dataclass(frozen=True)
class MonthlySchedulingMetrics:
    completed_lessons: int
    total_lessons: int
    earned_cents: int
    projected_cents: int


def monthly_scheduling_metrics(
    *,
    bookings: list[LessonBooking],
    payments: list[StudentPayment],
    allocations: list[LessonPaymentAllocation],
    timezone: str,
    now: datetime,
) -> MonthlySchedulingMetrics:
    local_now = now.astimezone(ZoneInfo(timezone))
    month_start = datetime(local_now.year, local_now.month, 1, tzinfo=local_now.tzinfo)
    if local_now.month == 12:
        next_month = datetime(local_now.year + 1, 1, 1, tzinfo=local_now.tzinfo)
    else:
        next_month = datetime(local_now.year, local_now.month + 1, 1, tzinfo=local_now.tzinfo)
    month_start_utc = month_start.astimezone(UTC)
    next_month_utc = next_month.astimezone(UTC)
    monthly_bookings = [
        booking
        for booking in bookings
        if booking.status == "confirmed"
        and month_start_utc <= booking.starts_at < next_month_utc
    ]
    completed = [booking for booking in monthly_bookings if booking.ends_at <= now]

    payment_by_id = {payment.id: payment for payment in payments}
    payment_id_by_booking_id = {
        allocation.booking_id: allocation.payment_id for allocation in allocations
    }

    def recognized_value(items: list[LessonBooking]) -> int:
        total = Decimal(0)
        for booking in items:
            payment_id = payment_id_by_booking_id.get(booking.id)
            payment = payment_by_id.get(payment_id)
            if payment is None or payment.amount_cents is None or payment.lessons_purchased <= 0:
                continue
            total += Decimal(payment.amount_cents) / Decimal(payment.lessons_purchased)
        return int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    return MonthlySchedulingMetrics(
        completed_lessons=len(completed),
        total_lessons=len(monthly_bookings),
        earned_cents=recognized_value(completed),
        projected_cents=recognized_value(monthly_bookings),
    )
