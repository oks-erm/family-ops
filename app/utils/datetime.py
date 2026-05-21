from datetime import datetime
from zoneinfo import ZoneInfo


def now_in_timezone(timezone: str) -> datetime:
    return datetime.now(ZoneInfo(timezone))

