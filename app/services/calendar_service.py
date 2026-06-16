from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import CalendarProvider
from app.db.repositories.calendar import CalendarRepository
from app.services.planning_service import CalendarEventInput


class CalendarService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = CalendarRepository(session)

    async def list_events_for_day(
        self,
        *,
        household_id: UUID,
        day: date,
        timezone: str,
    ) -> list[CalendarEventInput]:
        tz = ZoneInfo(timezone)
        start = datetime.combine(day, time.min, tzinfo=tz)
        end = start + timedelta(days=1)
        events = await self.repository.list_events_between(
            household_id=household_id,
            starts_before=end,
            ends_after=start,
        )
        return [
            CalendarEventInput(
                title=event.title,
                starts_at=event.starts_at.astimezone(tz),
                ends_at=event.ends_at.astimezone(tz),
                location=event.location,
            )
            for event in events
        ]

    async def sync_ical_feeds(self, *, household_id: UUID | None = None) -> int:
        feeds = await self.repository.list_active_ical_feeds(household_id=household_id)
        synced = 0
        async with httpx.AsyncClient(timeout=12) as client:
            for feed in feeds:
                response = await client.get(feed.url)
                response.raise_for_status()
                for event in self._parse_ics_events(response.text):
                    await self.repository.upsert_event(
                        household_id=feed.household_id,
                        user_id=feed.user_id,
                        source_type=CalendarProvider.ical,
                        source_id=feed.id,
                        external_event_id=event["uid"],
                        title=event["summary"],
                        starts_at=event["starts_at"],
                        ends_at=event["ends_at"],
                        location=event.get("location"),
                        raw_event=event,
                    )
                    synced += 1
        return synced

    async def sync_google_connections(self, *, household_id: UUID | None = None) -> int:
        connections = await self.repository.list_google_connections(household_id=household_id)
        synced = 0
        now = datetime.now(UTC)
        time_min = (now - timedelta(days=7)).isoformat().replace("+00:00", "Z")
        time_max = (now + timedelta(days=45)).isoformat().replace("+00:00", "Z")
        async with httpx.AsyncClient(timeout=12) as client:
            for connection in connections:
                access_token = await self._google_access_token(client=client, connection=connection)
                if not access_token:
                    continue
                response = await client.get(
                    "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                    params={
                        "singleEvents": "true",
                        "orderBy": "startTime",
                        "timeMin": time_min,
                        "timeMax": time_max,
                    },
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                for item in response.json().get("items", []):
                    parsed = self._google_event_from_raw(item)
                    if parsed is None:
                        continue
                    await self.repository.upsert_event(
                        household_id=connection.household_id,
                        user_id=connection.user_id,
                        source_type=CalendarProvider.google,
                        source_id=connection.id,
                        external_event_id=parsed["uid"],
                        title=parsed["summary"],
                        starts_at=parsed["starts_at"],
                        ends_at=parsed["ends_at"],
                        location=parsed.get("location"),
                        raw_event=item,
                    )
                    synced += 1
        return synced

    async def _google_access_token(self, *, client: httpx.AsyncClient, connection) -> str | None:
        if not connection.access_token and not connection.refresh_token:
            return None
        expires_at = connection.token_expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if connection.access_token and (expires_at is None or expires_at > datetime.now(UTC) + timedelta(minutes=2)):
            return connection.access_token
        if not connection.refresh_token:
            return connection.access_token

        settings = get_settings()
        if not settings.google_client_id or not settings.google_client_secret:
            return connection.access_token
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": connection.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        response.raise_for_status()
        token_data = response.json()
        connection.access_token = token_data.get("access_token") or connection.access_token
        expires_in = int(token_data.get("expires_in") or 0)
        if expires_in:
            connection.token_expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
        await self.session.commit()
        await self.session.refresh(connection)
        return connection.access_token

    def _parse_ics_events(self, ics_text: str) -> list[dict[str, object]]:
        unfolded = self._unfold_ics(ics_text)
        events: list[dict[str, object]] = []
        current: dict[str, str] | None = None
        for line in unfolded.splitlines():
            line = line.strip()
            if line == "BEGIN:VEVENT":
                current = {}
                continue
            if line == "END:VEVENT" and current is not None:
                parsed = self._event_from_raw(current)
                if parsed is not None:
                    events.append(parsed)
                current = None
                continue
            if current is None or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.split(";", 1)[0].upper()
            current[key] = self._clean_ics_text(value)
        return events

    @staticmethod
    def _unfold_ics(ics_text: str) -> str:
        lines: list[str] = []
        for raw_line in ics_text.replace("\r\n", "\n").split("\n"):
            if raw_line.startswith((" ", "\t")) and lines:
                lines[-1] += raw_line[1:]
            else:
                lines.append(raw_line)
        return "\n".join(lines)

    def _event_from_raw(self, raw: dict[str, str]) -> dict[str, object] | None:
        uid = raw.get("UID")
        summary = raw.get("SUMMARY") or "Calendar event"
        starts_at_raw = raw.get("DTSTART")
        if not uid or not starts_at_raw:
            return None
        starts_at = self._parse_ics_datetime(starts_at_raw)
        ends_at = self._parse_ics_datetime(raw.get("DTEND") or starts_at_raw)
        if ends_at <= starts_at:
            ends_at = starts_at + timedelta(hours=1)
        return {
            "uid": uid,
            "summary": summary,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "location": raw.get("LOCATION"),
        }

    @staticmethod
    def _parse_ics_datetime(value: str) -> datetime:
        if len(value) == 8 and value.isdigit():
            return datetime.strptime(value, "%Y%m%d").replace(tzinfo=ZoneInfo("UTC"))
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
        return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo("UTC"))

    @staticmethod
    def _clean_ics_text(value: str) -> str:
        return value.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").strip()

    def _google_event_from_raw(self, raw: dict[str, object]) -> dict[str, object] | None:
        event_id = str(raw.get("id") or "")
        if not event_id:
            return None
        start = raw.get("start") if isinstance(raw.get("start"), dict) else {}
        end = raw.get("end") if isinstance(raw.get("end"), dict) else {}
        starts_at = self._parse_google_datetime(start)
        ends_at = self._parse_google_datetime(end)
        if starts_at is None:
            return None
        if ends_at is None or ends_at <= starts_at:
            ends_at = starts_at + timedelta(hours=1)
        return {
            "uid": event_id,
            "summary": str(raw.get("summary") or "Calendar event"),
            "starts_at": starts_at,
            "ends_at": ends_at,
            "location": str(raw.get("location") or "") or None,
        }

    @staticmethod
    def _parse_google_datetime(value: dict[str, object]) -> datetime | None:
        raw = value.get("dateTime") or value.get("date")
        if not raw:
            return None
        text = str(raw)
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("UTC"))
        if text.endswith("Z"):
            text = text.replace("Z", "+00:00")
        return datetime.fromisoformat(text)
