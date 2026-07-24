import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class SchedulingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class BusyPeriod:
    starts_at: datetime
    ends_at: datetime
    requires_buffer: bool = True


def validate_timezone(value: str) -> str:
    timezone = value.strip()
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise SchedulingValidationError("Choose a valid IANA timezone.") from exc
    return timezone


def normalize_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")
    if not 3 <= len(slug) <= 100:
        raise SchedulingValidationError("Booking link must contain 3 to 100 letters or numbers.")
    return slug


def periods_overlap(
    first_start: datetime,
    first_end: datetime,
    second_start: datetime,
    second_end: datetime,
) -> bool:
    return first_start < second_end and first_end > second_start


def generate_slots(
    *,
    day: date,
    timezone: str,
    rules: list[tuple[int, time, time]],
    duration_minutes: int,
    interval_minutes: int,
    buffer_before_minutes: int,
    buffer_after_minutes: int,
    earliest_start: datetime,
    latest_start: datetime,
    busy_periods: list[BusyPeriod],
) -> list[datetime]:
    tz = ZoneInfo(timezone)
    slots: list[datetime] = []
    duration = timedelta(minutes=duration_minutes)
    interval = timedelta(minutes=interval_minutes)
    buffer_before = timedelta(minutes=buffer_before_minutes)
    buffer_after = timedelta(minutes=buffer_after_minutes)
    for weekday, starts_at, ends_at in rules:
        if weekday != day.weekday() or ends_at <= starts_at:
            continue
        cursor = datetime.combine(day, starts_at, tzinfo=tz)
        window_end = datetime.combine(day, ends_at, tzinfo=tz)
        while cursor + duration <= window_end:
            if earliest_start <= cursor <= latest_start:
                if not any(
                    periods_overlap(
                        cursor - buffer_before if busy.requires_buffer else cursor,
                        (
                            cursor + duration + buffer_after
                            if busy.requires_buffer
                            else cursor + duration
                        ),
                        busy.starts_at,
                        busy.ends_at,
                    )
                    for busy in busy_periods
                ):
                    slots.append(cursor)
            cursor += interval
    return slots
