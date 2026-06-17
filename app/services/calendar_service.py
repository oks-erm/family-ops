from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import CalendarProvider
from app.db.repositories.calendar import CalendarRepository
from app.services.planning_service import CalendarEventInput


class CalendarNotConnectedError(RuntimeError):
    pass


class CalendarWritePermissionError(RuntimeError):
    pass


class CalendarEventMatchError(RuntimeError):
    pass


class CalendarSyncError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
        settings = get_settings()
        async with httpx.AsyncClient(timeout=12) as client:
            for connection in connections:
                access_token = await self._google_access_token(client=client, connection=connection)
                if not access_token:
                    continue
                calendar_id = (connection.external_account_id or settings.google_calendar_id or "primary").strip()
                calendar_path = quote(calendar_id, safe="")
                response = await client.get(
                    f"https://www.googleapis.com/calendar/v3/calendars/{calendar_path}/events",
                    params={
                        "singleEvents": "true",
                        "orderBy": "startTime",
                        "timeMin": time_min,
                        "timeMax": time_max,
                    },
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if not response.is_success:
                    raise self._google_sync_error(response=response, calendar_id=calendar_id)
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

    @staticmethod
    def _google_sync_error(*, response: httpx.Response, calendar_id: str) -> CalendarSyncError:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        google_message = str(payload.get("error", {}).get("message") or "").strip()
        if response.status_code == 401:
            message = "Google Calendar token expired. Reconnect Google Calendar from the dashboard."
        elif response.status_code == 403:
            if "not been used" in google_message or "disabled" in google_message:
                message = (
                    "Google Calendar API is disabled in Google Cloud. Enable Google Calendar API "
                    "for project 960300907926, wait a few minutes, then press Sync now."
                )
            else:
                message = (
                    "Google Calendar denied access. Check that the connected Google account can access "
                    f"{calendar_id}, then reconnect Google Calendar."
                )
        elif response.status_code == 404:
            message = (
                f"Google Calendar ID was not found: {calendar_id}. Check the calendar ID or share "
                "that calendar with the connected Google account."
            )
        else:
            message = f"Google Calendar sync failed with status {response.status_code}."
        if google_message and response.status_code != 403:
            message = f"{message} Google says: {google_message}"
        return CalendarSyncError(message, status_code=response.status_code)

    async def create_google_event(
        self,
        *,
        household_id: UUID,
        user_id: UUID,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        timezone: str,
        location: str | None = None,
    ) -> CalendarEventInput:
        connection = await self._google_write_connection(household_id=household_id)
        calendar_id = self._connection_calendar_id(connection)
        body = {
            "summary": title,
            "start": {"dateTime": starts_at.isoformat(), "timeZone": timezone},
            "end": {"dateTime": ends_at.isoformat(), "timeZone": timezone},
        }
        if location:
            body["location"] = location
        async with httpx.AsyncClient(timeout=12) as client:
            access_token = await self._google_access_token(client=client, connection=connection)
            if not access_token:
                raise CalendarNotConnectedError("Google Calendar is not connected.")
            response = await client.post(
                self._google_events_url(calendar_id),
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self._raise_for_google_write(response)
            raw = response.json()
        parsed = self._google_event_from_raw(raw)
        if parsed is None:
            raise CalendarEventMatchError("Google returned an event I could not parse.")
        await self.repository.upsert_event(
            household_id=household_id,
            user_id=user_id,
            source_type=CalendarProvider.google,
            source_id=connection.id,
            external_event_id=parsed["uid"],
            title=parsed["summary"],
            starts_at=parsed["starts_at"],
            ends_at=parsed["ends_at"],
            location=parsed.get("location"),
            raw_event=raw,
        )
        return CalendarEventInput(
            title=parsed["summary"],
            starts_at=parsed["starts_at"],
            ends_at=parsed["ends_at"],
            location=parsed.get("location"),
        )

    async def update_google_event(
        self,
        *,
        household_id: UUID,
        user_id: UUID,
        title: str,
        day: date,
        timezone: str,
        new_title: str | None = None,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> CalendarEventInput:
        connection = await self._google_write_connection(household_id=household_id)
        event = await self._single_google_event_match(
            household_id=household_id,
            title=title,
            day=day,
            timezone=timezone,
            source_id=connection.id,
        )
        calendar_id = self._connection_calendar_id(connection)
        body: dict[str, object] = {}
        if new_title:
            body["summary"] = new_title
        if starts_at is not None and ends_at is not None:
            body["start"] = {"dateTime": starts_at.isoformat(), "timeZone": timezone}
            body["end"] = {"dateTime": ends_at.isoformat(), "timeZone": timezone}
        if not body:
            raise CalendarEventMatchError("No calendar changes were provided.")
        async with httpx.AsyncClient(timeout=12) as client:
            access_token = await self._google_access_token(client=client, connection=connection)
            if not access_token:
                raise CalendarNotConnectedError("Google Calendar is not connected.")
            response = await client.patch(
                f"{self._google_events_url(calendar_id)}/{quote(event.external_event_id, safe='')}",
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self._raise_for_google_write(response)
            raw = response.json()
        parsed = self._google_event_from_raw(raw)
        if parsed is None:
            raise CalendarEventMatchError("Google returned an event I could not parse.")
        await self.repository.upsert_event(
            household_id=household_id,
            user_id=user_id,
            source_type=CalendarProvider.google,
            source_id=connection.id,
            external_event_id=parsed["uid"],
            title=parsed["summary"],
            starts_at=parsed["starts_at"],
            ends_at=parsed["ends_at"],
            location=parsed.get("location"),
            raw_event=raw,
        )
        return CalendarEventInput(
            title=parsed["summary"],
            starts_at=parsed["starts_at"],
            ends_at=parsed["ends_at"],
            location=parsed.get("location"),
        )

    async def delete_google_event(
        self,
        *,
        household_id: UUID,
        title: str,
        day: date,
        timezone: str,
    ) -> CalendarEventInput:
        connection = await self._google_write_connection(household_id=household_id)
        event = await self._single_google_event_match(
            household_id=household_id,
            title=title,
            day=day,
            timezone=timezone,
            source_id=connection.id,
        )
        calendar_id = self._connection_calendar_id(connection)
        deleted = CalendarEventInput(
            title=event.title,
            starts_at=event.starts_at.astimezone(ZoneInfo(timezone)),
            ends_at=event.ends_at.astimezone(ZoneInfo(timezone)),
            location=event.location,
        )
        async with httpx.AsyncClient(timeout=12) as client:
            access_token = await self._google_access_token(client=client, connection=connection)
            if not access_token:
                raise CalendarNotConnectedError("Google Calendar is not connected.")
            response = await client.delete(
                f"{self._google_events_url(calendar_id)}/{quote(event.external_event_id, safe='')}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self._raise_for_google_write(response)
        await self.repository.delete_cached_event(event=event)
        return deleted

    async def _google_write_connection(self, *, household_id: UUID):
        connection = await self.repository.first_google_connection(household_id=household_id)
        if connection is None:
            raise CalendarNotConnectedError("Google Calendar is not connected.")
        scopes = set(connection.scopes or [])
        if "https://www.googleapis.com/auth/calendar.events" not in scopes:
            raise CalendarWritePermissionError("Google Calendar needs to be reconnected with event access.")
        return connection

    def _connection_calendar_id(self, connection) -> str:
        settings = get_settings()
        return (connection.external_account_id or settings.google_calendar_id or "primary").strip()

    def _google_events_url(self, calendar_id: str) -> str:
        return f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}/events"

    async def _single_google_event_match(
        self,
        *,
        household_id: UUID,
        title: str,
        day: date,
        timezone: str,
        source_id: UUID,
    ):
        tz = ZoneInfo(timezone)
        starts_before = datetime.combine(day, time.min, tzinfo=tz) + timedelta(days=1)
        ends_after = datetime.combine(day, time.min, tzinfo=tz)
        events = await self.repository.list_events_between(
            household_id=household_id,
            starts_before=starts_before,
            ends_after=ends_after,
        )
        target = self._normalize_title(title)
        matches = [
            event
            for event in events
            if event.source_type == CalendarProvider.google
            and event.source_id == source_id
            and (
                target in self._normalize_title(event.title)
                or self._normalize_title(event.title) in target
            )
        ]
        if not matches:
            raise CalendarEventMatchError(f"I could not find '{title}' on {day.isoformat()}.")
        if len(matches) > 1:
            options = ", ".join(
                f"{event.title} at {event.starts_at.astimezone(tz).strftime('%H:%M')}"
                for event in matches[:5]
            )
            raise CalendarEventMatchError(f"I found more than one matching event: {options}. Please be more specific.")
        return matches[0]

    @staticmethod
    def _normalize_title(value: str) -> str:
        return " ".join("".join(char.lower() if char.isalnum() else " " for char in value).split())

    @staticmethod
    def _raise_for_google_write(response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise CalendarWritePermissionError("Google Calendar needs to be reconnected with event access.")
        response.raise_for_status()

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
        if not response.is_success:
            raise CalendarSyncError(
                "Google Calendar token refresh failed. Reconnect Google Calendar from the dashboard.",
                status_code=response.status_code,
            )
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
