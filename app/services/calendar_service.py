import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import quote, urljoin, urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import recurring_ical_events
from icalendar import Calendar
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    CalendarConnection,
    CalendarEventCache,
    CalendarProvider,
    SchedulingCalendar,
)
from app.db.repositories.calendar import CalendarRepository
from app.services.credential_cipher import CredentialCipher, CredentialDecryptionError
from app.services.planning_service import CalendarEventInput
from app.utils.urls import UnsafeExternalURLError, validate_public_https_url

logger = logging.getLogger(__name__)


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


class ICloudAuthenticationError(CalendarSyncError):
    pass


class CalDAVProtocolError(CalendarSyncError):
    pass


class CalendarService:
    _DAV = "DAV:"
    _CALDAV = "urn:ietf:params:xml:ns:caldav"
    _ICLOUD_ROOT = "https://caldav.icloud.com/"

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

    async def sync_ical_feeds(
        self,
        *,
        household_id: UUID | None = None,
        feed_id: UUID | None = None,
    ) -> int:
        feeds = await self.repository.list_active_ical_feeds(
            household_id=household_id,
            feed_id=feed_id,
        )
        synced = 0
        now = datetime.now(UTC)
        range_start = now - timedelta(days=7)
        range_end = now + timedelta(days=45)
        first_error: Exception | None = None
        async with httpx.AsyncClient(timeout=12) as client:
            for feed in feeds:
                try:
                    safe_url = await validate_public_https_url(feed.url)
                    response = await client.get(safe_url, follow_redirects=False)
                    response.raise_for_status()
                    seen: set[str] = set()
                    for event in self._parse_ics_events(
                        response.text,
                        range_start=range_start,
                        range_end=range_end,
                    ):
                        seen.add(str(event["uid"]))
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
                            raw_event={
                                **event,
                                "starts_at": event["starts_at"].isoformat(),
                                "ends_at": event["ends_at"].isoformat(),
                            },
                            commit=False,
                        )
                        synced += 1
                    await self.repository.remove_missing_ical_events(
                        source_id=feed.id,
                        seen_external_ids=seen,
                        starts_before=range_end,
                        ends_after=range_start,
                    )
                except (httpx.HTTPError, UnsafeExternalURLError, ValueError) as exc:
                    logger.warning(
                        "iCal feed sync failed for feed %s: %s", feed.id, type(exc).__name__
                    )
                    first_error = first_error or exc
        if first_error is not None:
            raise first_error
        return synced

    async def sync_google_connections(
        self,
        *,
        household_id: UUID | None = None,
        connection_id: UUID | None = None,
    ) -> int:
        connections = await self.repository.list_google_connections(household_id=household_id)
        if connection_id is not None:
            connections = [item for item in connections if item.id == connection_id]
        synced = 0
        now = datetime.now(UTC)
        time_min = (now - timedelta(days=7)).isoformat().replace("+00:00", "Z")
        time_max = (now + timedelta(days=45)).isoformat().replace("+00:00", "Z")
        settings = get_settings()
        first_error: Exception | None = None
        async with httpx.AsyncClient(timeout=12) as client:
            for connection in connections:
                try:
                    access_token = await self._google_access_token(
                        client=client,
                        connection=connection,
                    )
                    if not access_token:
                        continue
                    result = await self.session.execute(
                        select(SchedulingCalendar).where(
                            SchedulingCalendar.connection_id == connection.id,
                            SchedulingCalendar.include_in_conflicts.is_(True),
                        )
                    )
                    selected = list(result.scalars().all())
                    calendar_ids = [item.external_calendar_id for item in selected]
                    if not calendar_ids:
                        calendar_ids = [
                            (
                                connection.external_account_id
                                or settings.google_calendar_id
                                or "primary"
                            ).strip()
                        ]
                    for calendar_id in dict.fromkeys(calendar_ids):
                        try:
                            synced += await self._sync_google_calendar(
                                client=client,
                                connection=connection,
                                access_token=access_token,
                                calendar_id=calendar_id,
                                time_min=time_min,
                                time_max=time_max,
                                starts_before=now + timedelta(days=45),
                                ends_after=now - timedelta(days=7),
                            )
                        except (CalendarSyncError, httpx.HTTPError) as exc:
                            logger.warning(
                                "Google calendar sync failed for connection %s: %s",
                                connection.id,
                                type(exc).__name__,
                            )
                            first_error = first_error or exc
                except (CalendarSyncError, httpx.HTTPError) as exc:
                    logger.warning(
                        "Google account sync failed for connection %s: %s",
                        connection.id,
                        type(exc).__name__,
                    )
                    first_error = first_error or exc
        if first_error is not None:
            raise first_error
        return synced

    async def discover_icloud_calendars(
        self, *, profile, connection: CalendarConnection
    ) -> int:
        password = self._icloud_password(connection)
        async with httpx.AsyncClient(timeout=15) as client:
            principal_response = await self._caldav_request(
                client=client,
                connection=connection,
                password=password,
                method="PROPFIND",
                url=self._ICLOUD_ROOT,
                depth="0",
                body=(
                    '<?xml version="1.0" encoding="utf-8"?>'
                    '<d:propfind xmlns:d="DAV:"><d:prop>'
                    "<d:current-user-principal/>"
                    "</d:prop></d:propfind>"
                ),
            )
            principal_href = self._xml_property_href(
                principal_response.text, f"{{{self._DAV}}}current-user-principal"
            )
            principal_url = self._icloud_url(self._ICLOUD_ROOT, principal_href)
            home_response = await self._caldav_request(
                client=client,
                connection=connection,
                password=password,
                method="PROPFIND",
                url=principal_url,
                depth="0",
                body=(
                    '<?xml version="1.0" encoding="utf-8"?>'
                    '<d:propfind xmlns:d="DAV:" '
                    'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
                    "<c:calendar-home-set/>"
                    "</d:prop></d:propfind>"
                ),
            )
            home_href = self._xml_property_href(
                home_response.text, f"{{{self._CALDAV}}}calendar-home-set"
            )
            home_url = self._icloud_url(principal_url, home_href)
            calendars_response = await self._caldav_request(
                client=client,
                connection=connection,
                password=password,
                method="PROPFIND",
                url=home_url,
                depth="1",
                body=(
                    '<?xml version="1.0" encoding="utf-8"?>'
                    '<d:propfind xmlns:d="DAV:" '
                    'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
                    "<d:displayname/><d:resourcetype/>"
                    "<c:supported-calendar-component-set/>"
                    "</d:prop></d:propfind>"
                ),
            )

        discovered = 0
        discovered_urls: set[str] = set()
        for response in self._multistatus_responses(calendars_response.text):
            properties = self._successful_properties(response)
            resource_type = properties.find(f"{{{self._DAV}}}resourcetype")
            if resource_type is None or resource_type.find(
                f"{{{self._CALDAV}}}calendar"
            ) is None:
                continue
            supported = properties.find(
                f"{{{self._CALDAV}}}supported-calendar-component-set"
            )
            components = {
                str(item.attrib.get("name") or "").upper()
                for item in supported or []
            }
            if components and "VEVENT" not in components:
                continue
            href = response.findtext(f"{{{self._DAV}}}href") or ""
            calendar_url = self._icloud_url(home_url, href)
            discovered_urls.add(calendar_url)
            display_name = properties.findtext(f"{{{self._DAV}}}displayname") or "iCloud"
            result = await self.session.execute(
                select(SchedulingCalendar).where(
                    SchedulingCalendar.profile_id == profile.id,
                    SchedulingCalendar.connection_id == connection.id,
                    SchedulingCalendar.external_calendar_id == calendar_url,
                )
            )
            calendar = result.scalar_one_or_none()
            if calendar is None:
                calendar = SchedulingCalendar(
                    profile_id=profile.id,
                    connection_id=connection.id,
                    external_calendar_id=calendar_url,
                    name=display_name.strip() or "iCloud",
                    access_role="read",
                    include_in_conflicts=True,
                    can_write=False,
                )
                self.session.add(calendar)
            else:
                calendar.name = display_name.strip() or "iCloud"
            discovered += 1
        if discovered_urls:
            existing_result = await self.session.execute(
                select(SchedulingCalendar).where(
                    SchedulingCalendar.profile_id == profile.id,
                    SchedulingCalendar.connection_id == connection.id,
                )
            )
            for calendar in existing_result.scalars().all():
                if calendar.external_calendar_id in discovered_urls:
                    continue
                await self.repository.delete_calendar_events(
                    provider=CalendarProvider.icloud,
                    source_id=connection.id,
                    calendar_id=calendar.external_calendar_id,
                )
                await self.session.delete(calendar)
        connection.external_account_id = home_url
        await self.session.commit()
        return discovered

    async def sync_icloud_connections(
        self,
        *,
        household_id: UUID | None = None,
        connection_id: UUID | None = None,
    ) -> int:
        connections = await self.repository.list_icloud_connections(household_id=household_id)
        if connection_id is not None:
            connections = [item for item in connections if item.id == connection_id]
        now = datetime.now(UTC)
        range_start = now - timedelta(days=7)
        range_end = now + timedelta(days=45)
        synced = 0
        first_error: Exception | None = None
        async with httpx.AsyncClient(timeout=20) as client:
            for connection in connections:
                try:
                    result = await self.session.execute(
                        select(SchedulingCalendar).where(
                            SchedulingCalendar.connection_id == connection.id,
                            SchedulingCalendar.include_in_conflicts.is_(True),
                        )
                    )
                    calendars = list(result.scalars().all())
                    if not calendars:
                        continue
                    password = self._icloud_password(connection)
                    for calendar in calendars:
                        synced += await self._sync_icloud_calendar(
                            client=client,
                            connection=connection,
                            password=password,
                            calendar=calendar,
                            range_start=range_start,
                            range_end=range_end,
                        )
                except (CalendarSyncError, httpx.HTTPError, ValueError) as exc:
                    logger.warning(
                        "iCloud calendar sync failed for connection %s: %s",
                        connection.id,
                        type(exc).__name__,
                    )
                    first_error = first_error or exc
        if first_error is not None:
            raise first_error
        return synced

    async def _sync_icloud_calendar(
        self,
        *,
        client: httpx.AsyncClient,
        connection: CalendarConnection,
        password: str,
        calendar: SchedulingCalendar,
        range_start: datetime,
        range_end: datetime,
    ) -> int:
        start_text = range_start.strftime("%Y%m%dT%H%M%SZ")
        end_text = range_end.strftime("%Y%m%dT%H%M%SZ")
        response = await self._caldav_request(
            client=client,
            connection=connection,
            password=password,
            method="REPORT",
            url=calendar.external_calendar_id,
            depth="1",
            body=(
                '<?xml version="1.0" encoding="utf-8"?>'
                '<c:calendar-query xmlns:d="DAV:" '
                'xmlns:c="urn:ietf:params:xml:ns:caldav">'
                "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
                '<c:filter><c:comp-filter name="VCALENDAR">'
                '<c:comp-filter name="VEVENT">'
                f'<c:time-range start="{start_text}" end="{end_text}"/>'
                "</c:comp-filter></c:comp-filter></c:filter>"
                "</c:calendar-query>"
            ),
        )
        seen: set[str] = set()
        synced = 0
        for item in self._multistatus_responses(response.text):
            properties = self._successful_properties(item)
            calendar_data = properties.findtext(f"{{{self._CALDAV}}}calendar-data")
            if not calendar_data:
                continue
            href = item.findtext(f"{{{self._DAV}}}href") or ""
            for event in self._parse_ics_events(
                calendar_data,
                range_start=range_start,
                range_end=range_end,
            ):
                external_id = f"{calendar.external_calendar_id}:{event['uid']}"
                seen.add(external_id)
                await self.repository.upsert_event(
                    household_id=connection.household_id,
                    user_id=connection.user_id,
                    source_type=CalendarProvider.icloud,
                    source_id=connection.id,
                    external_event_id=external_id,
                    title=event["summary"],
                    starts_at=event["starts_at"],
                    ends_at=event["ends_at"],
                    location=event.get("location"),
                    raw_event={
                        "_calendar_id": calendar.external_calendar_id,
                        "_href": href,
                    },
                    commit=False,
                )
                synced += 1
        await self.repository.remove_missing_calendar_events(
            provider=CalendarProvider.icloud,
            source_id=connection.id,
            calendar_id=calendar.external_calendar_id,
            seen_external_ids=seen,
            starts_before=range_end,
            ends_after=range_start,
        )
        return synced

    def _icloud_password(self, connection: CalendarConnection) -> str:
        try:
            return CredentialCipher().decrypt(connection.access_token)
        except CredentialDecryptionError as exc:
            raise ICloudAuthenticationError(
                "The iCloud credential must be reconnected."
            ) from exc

    async def _caldav_request(
        self,
        *,
        client: httpx.AsyncClient,
        connection: CalendarConnection,
        password: str,
        method: str,
        url: str,
        depth: str,
        body: str,
    ) -> httpx.Response:
        target = self._icloud_url(self._ICLOUD_ROOT, url)
        for _ in range(6):
            response = await client.request(
                method,
                target,
                content=body.encode(),
                headers={"Content-Type": "application/xml; charset=utf-8", "Depth": depth},
                auth=httpx.BasicAuth(connection.account_email or "", password),
                follow_redirects=False,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise CalDAVProtocolError("iCloud returned an invalid redirect.")
                target = self._icloud_url(target, location)
                continue
            if response.status_code in {401, 403}:
                raise ICloudAuthenticationError(
                    "Apple rejected the account email or app-specific password.",
                    status_code=response.status_code,
                )
            if response.status_code not in {200, 207}:
                raise CalDAVProtocolError(
                    "iCloud CalDAV request failed.", status_code=response.status_code
                )
            return response
        raise CalDAVProtocolError("iCloud redirected too many times.")

    @staticmethod
    def _icloud_url(base: str, href: str) -> str:
        value = urljoin(base, href)
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not (
            hostname == "icloud.com" or hostname.endswith(".icloud.com")
        ):
            raise CalDAVProtocolError("iCloud returned an unsafe CalDAV URL.")
        return value

    @classmethod
    def _multistatus_responses(cls, xml_text: str) -> list[ET.Element]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise CalDAVProtocolError("iCloud returned invalid CalDAV XML.") from exc
        return list(root.findall(f"{{{cls._DAV}}}response"))

    @classmethod
    def _successful_properties(cls, response: ET.Element) -> ET.Element:
        for propstat in response.findall(f"{{{cls._DAV}}}propstat"):
            status = propstat.findtext(f"{{{cls._DAV}}}status") or ""
            if " 200 " in status:
                properties = propstat.find(f"{{{cls._DAV}}}prop")
                if properties is not None:
                    return properties
        raise CalDAVProtocolError("iCloud CalDAV response did not contain properties.")

    @classmethod
    def _xml_property_href(cls, xml_text: str, property_tag: str) -> str:
        responses = cls._multistatus_responses(xml_text)
        if not responses:
            raise CalDAVProtocolError("iCloud CalDAV response was empty.")
        properties = cls._successful_properties(responses[0])
        property_node = properties.find(property_tag)
        href = (
            property_node.findtext(f"{{{cls._DAV}}}href")
            if property_node is not None
            else None
        )
        if not href:
            raise CalDAVProtocolError("iCloud CalDAV discovery was incomplete.")
        return href

    async def _sync_google_calendar(
        self,
        *,
        client: httpx.AsyncClient,
        connection,
        access_token: str,
        calendar_id: str,
        time_min: str,
        time_max: str,
        starts_before: datetime,
        ends_after: datetime,
    ) -> int:
        calendar_path = quote(calendar_id, safe="")
        page_token: str | None = None
        seen: set[str] = set()
        synced = 0
        while True:
            params = {
                "singleEvents": "true",
                "orderBy": "startTime",
                "timeMin": time_min,
                "timeMax": time_max,
                "maxResults": "2500",
            }
            if page_token:
                params["pageToken"] = page_token
            response = await client.get(
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_path}/events",
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if not response.is_success:
                raise self._google_sync_error(response=response, calendar_id=calendar_id)
            payload = response.json()
            for item in payload.get("items", []):
                if item.get("status") == "cancelled":
                    continue
                parsed = self._google_event_from_raw(item)
                if parsed is None:
                    continue
                external_id = f"{calendar_id}:{parsed['uid']}"
                seen.add(external_id)
                raw_event = {**item, "_calendar_id": calendar_id}
                await self.repository.upsert_event(
                    household_id=connection.household_id,
                    user_id=connection.user_id,
                    source_type=CalendarProvider.google,
                    source_id=connection.id,
                    external_event_id=external_id,
                    title=parsed["summary"],
                    starts_at=parsed["starts_at"],
                    ends_at=parsed["ends_at"],
                    location=parsed.get("location"),
                    raw_event=raw_event,
                    commit=False,
                )
                synced += 1
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        await self.repository.remove_missing_google_events(
            source_id=connection.id,
            calendar_id=calendar_id,
            seen_external_ids=seen,
            starts_before=starts_before,
            ends_after=ends_after,
        )
        await self.repository.remove_legacy_google_events(
            source_id=connection.id,
            starts_before=starts_before,
            ends_after=ends_after,
        )
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
        calendar_id_override: str | None = None,
        description: str | None = None,
        attendee_email: str | None = None,
        conference_data: dict[str, object] | None = None,
        create_google_meet: bool = False,
    ) -> CalendarEventInput:
        if conference_data is not None and create_google_meet:
            raise ValueError("Provide existing conference data or request a new Google Meet.")
        connection = await self._google_write_connection(
            household_id=household_id,
            calendar_id=calendar_id_override,
        )
        calendar_id = calendar_id_override or self._connection_calendar_id(connection)
        body = {
            "summary": title,
            "start": {"dateTime": starts_at.isoformat(), "timeZone": timezone},
            "end": {"dateTime": ends_at.isoformat(), "timeZone": timezone},
        }
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        normalized_attendee = (attendee_email or "").strip().casefold()
        if create_google_meet:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": uuid4().hex,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
        elif conference_data is not None:
            body["conferenceData"] = conference_data
        if normalized_attendee and not create_google_meet:
            body["attendees"] = [{"email": normalized_attendee}]

        params: dict[str, str | int] = {}
        if create_google_meet or conference_data is not None:
            params["conferenceDataVersion"] = 1
        if normalized_attendee and not create_google_meet:
            params["sendUpdates"] = "all"

        async with httpx.AsyncClient(timeout=12) as client:
            access_token = await self._google_access_token(client=client, connection=connection)
            if not access_token:
                raise CalendarNotConnectedError("Google Calendar is not connected.")
            headers = {"Authorization": f"Bearer {access_token}"}
            response = await client.post(
                self._google_events_url(calendar_id),
                params=params,
                json=body,
                headers=headers,
            )
            self._raise_for_google_write(response)
            raw = response.json()
            if create_google_meet:
                raw = await self._wait_for_google_meet(
                    client=client,
                    calendar_id=calendar_id,
                    event_id=str(raw.get("id") or ""),
                    initial_event=raw,
                    headers=headers,
                )
                if normalized_attendee:
                    response = await client.patch(
                        (
                            f"{self._google_events_url(calendar_id)}/"
                            f"{quote(str(raw['id']), safe='')}"
                        ),
                        params={"conferenceDataVersion": 1, "sendUpdates": "all"},
                        json={"attendees": [{"email": normalized_attendee}]},
                        headers=headers,
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
            external_event_id=f"{calendar_id}:{parsed['uid']}",
            title=parsed["summary"],
            starts_at=parsed["starts_at"],
            ends_at=parsed["ends_at"],
            location=parsed.get("location"),
            raw_event={**raw, "_calendar_id": calendar_id},
        )
        return CalendarEventInput(
            title=parsed["summary"],
            starts_at=parsed["starts_at"],
            ends_at=parsed["ends_at"],
            location=parsed.get("location"),
            external_calendar_id=calendar_id,
            external_event_id=str(parsed["uid"]),
            meeting_url=self._google_meet_url(raw),
            conference_data=self._copyable_conference_data(raw),
        )

    async def _wait_for_google_meet(
        self,
        *,
        client: httpx.AsyncClient,
        calendar_id: str,
        event_id: str,
        initial_event: dict[str, object],
        headers: dict[str, str],
    ) -> dict[str, object]:
        if not event_id:
            raise CalendarEventMatchError("Google did not return an event ID.")
        event = initial_event
        event_url = f"{self._google_events_url(calendar_id)}/{quote(event_id, safe='')}"
        for attempt in range(6):
            conference_data = event.get("conferenceData")
            create_request = (
                conference_data.get("createRequest")
                if isinstance(conference_data, dict)
                else None
            )
            request_status = (
                create_request.get("status") if isinstance(create_request, dict) else None
            )
            status = str(
                request_status.get("statusCode", "")
                if isinstance(request_status, dict)
                else ""
            )
            if self._google_meet_url(event):
                return event
            if status == "failure":
                raise CalendarEventMatchError("Google could not create the Meet conference.")
            if attempt == 5:
                break
            await asyncio.sleep(0.5)
            response = await client.get(
                event_url,
                params={"conferenceDataVersion": 1},
                headers=headers,
            )
            self._raise_for_google_write(response)
            event = response.json()
        raise CalendarEventMatchError("Google Meet creation did not finish in time.")

    @staticmethod
    def _google_meet_url(event: dict[str, object]) -> str | None:
        candidates = [event.get("hangoutLink")]
        conference_data = event.get("conferenceData")
        if isinstance(conference_data, dict):
            entry_points = conference_data.get("entryPoints")
            if isinstance(entry_points, list):
                candidates.extend(
                    entry.get("uri")
                    for entry in entry_points
                    if isinstance(entry, dict) and entry.get("entryPointType") == "video"
                )
        for candidate in candidates:
            value = str(candidate or "").strip()
            parsed = urlsplit(value)
            if parsed.scheme == "https" and parsed.hostname == "meet.google.com":
                return value
        return None

    @classmethod
    def _copyable_conference_data(cls, event: dict[str, object]) -> dict[str, object] | None:
        conference_data = event.get("conferenceData")
        if not isinstance(conference_data, dict) or not cls._google_meet_url(event):
            return None
        # Google documents reuse by copying the entire conferenceData payload.
        return dict(conference_data)

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
                f"{self._google_events_url(calendar_id)}/{quote(self._provider_event_id(event.external_event_id), safe='')}",
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
            external_event_id=f"{calendar_id}:{parsed['uid']}",
            title=parsed["summary"],
            starts_at=parsed["starts_at"],
            ends_at=parsed["ends_at"],
            location=parsed.get("location"),
            raw_event={**raw, "_calendar_id": calendar_id},
        )
        return CalendarEventInput(
            title=parsed["summary"],
            starts_at=parsed["starts_at"],
            ends_at=parsed["ends_at"],
            location=parsed.get("location"),
        )

    async def delete_google_event_by_id(
        self,
        *,
        household_id: UUID,
        calendar_id: str | None,
        event_id: str,
    ) -> None:
        target_calendar_id = calendar_id or "primary"
        cached_external_id = f"{target_calendar_id}:{event_id}"
        cached_source = await self.session.execute(
            select(CalendarEventCache.source_id)
            .where(
                CalendarEventCache.household_id == household_id,
                CalendarEventCache.source_type == CalendarProvider.google,
                CalendarEventCache.external_event_id == cached_external_id,
            )
            .limit(1)
        )
        source_id = cached_source.scalar_one_or_none()
        connection = await self.session.get(CalendarConnection, source_id) if source_id else None
        if (
            connection is None
            or connection.household_id != household_id
            or connection.provider != CalendarProvider.google
        ):
            connection = await self._google_write_connection(
                household_id=household_id,
                calendar_id=calendar_id,
            )
            target_calendar_id = calendar_id or self._connection_calendar_id(connection)
            cached_external_id = f"{target_calendar_id}:{event_id}"
        scopes = set(connection.scopes or [])
        if "https://www.googleapis.com/auth/calendar.events" not in scopes:
            raise CalendarWritePermissionError(
                "Google Calendar needs to be reconnected with event access."
            )
        async with httpx.AsyncClient(timeout=12) as client:
            access_token = await self._google_access_token(client=client, connection=connection)
            if not access_token:
                raise CalendarNotConnectedError("Google Calendar is not connected.")
            response = await client.delete(
                f"{self._google_events_url(target_calendar_id)}/{quote(event_id, safe='')}",
                params={"sendUpdates": "all"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if response.status_code != 404:
                self._raise_for_google_write(response)
        await self.repository.delete_cached_event_by_external_id(
            source_id=connection.id,
            external_event_id=cached_external_id,
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
                f"{self._google_events_url(calendar_id)}/{quote(self._provider_event_id(event.external_event_id), safe='')}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self._raise_for_google_write(response)
        await self.repository.delete_cached_event(event=event)
        return deleted

    async def _google_write_connection(
        self,
        *,
        household_id: UUID,
        calendar_id: str | None = None,
    ):
        connection = None
        if calendar_id:
            result = await self.session.execute(
                select(CalendarConnection)
                .join(
                    SchedulingCalendar,
                    SchedulingCalendar.connection_id == CalendarConnection.id,
                )
                .where(
                    CalendarConnection.household_id == household_id,
                    SchedulingCalendar.external_calendar_id == calendar_id,
                    SchedulingCalendar.can_write.is_(True),
                )
                .limit(1)
            )
            connection = result.scalar_one_or_none()
        if connection is None:
            result = await self.session.execute(
                select(CalendarConnection)
                .join(
                    SchedulingCalendar,
                    SchedulingCalendar.connection_id == CalendarConnection.id,
                )
                .where(
                    CalendarConnection.household_id == household_id,
                    CalendarConnection.provider == CalendarProvider.google,
                    SchedulingCalendar.can_write.is_(True),
                )
                .order_by(CalendarConnection.created_at, SchedulingCalendar.created_at)
                .limit(1)
            )
            connection = result.scalar_one_or_none()
        if connection is None:
            connection = await self.repository.first_google_connection(household_id=household_id)
        if connection is None:
            raise CalendarNotConnectedError("Google Calendar is not connected.")
        scopes = set(connection.scopes or [])
        if "https://www.googleapis.com/auth/calendar.events" not in scopes:
            raise CalendarWritePermissionError(
                "Google Calendar needs to be reconnected with event access."
            )
        return connection

    def _connection_calendar_id(self, connection) -> str:
        settings = get_settings()
        return (connection.external_account_id or settings.google_calendar_id or "primary").strip()

    def _google_events_url(self, calendar_id: str) -> str:
        return (
            f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}/events"
        )

    @staticmethod
    def _provider_event_id(external_event_id: str) -> str:
        return external_event_id.rsplit(":", 1)[-1]

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
            raise CalendarEventMatchError(
                f"I found more than one matching event: {options}. Please be more specific."
            )
        return matches[0]

    @staticmethod
    def _normalize_title(value: str) -> str:
        return " ".join("".join(char.lower() if char.isalnum() else " " for char in value).split())

    @staticmethod
    def _raise_for_google_write(response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise CalendarWritePermissionError(
                "Google Calendar needs to be reconnected with event access."
            )
        response.raise_for_status()

    async def _google_access_token(self, *, client: httpx.AsyncClient, connection) -> str | None:
        if not connection.access_token and not connection.refresh_token:
            return None
        expires_at = connection.token_expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if connection.access_token and (
            expires_at is None or expires_at > datetime.now(UTC) + timedelta(minutes=2)
        ):
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

    async def google_access_token(self, *, client: httpx.AsyncClient, connection) -> str | None:
        return await self._google_access_token(client=client, connection=connection)

    def _parse_ics_events(
        self,
        ics_text: str,
        *,
        range_start: datetime,
        range_end: datetime,
    ) -> list[dict[str, object]]:
        calendar = Calendar.from_ical(ics_text)
        components = recurring_ical_events.of(calendar).between(range_start, range_end)
        events: list[dict[str, object]] = []
        default_tz = ZoneInfo(get_settings().default_timezone)
        for component in components:
            if str(component.get("STATUS") or "").upper() == "CANCELLED":
                continue
            uid = str(component.get("UID") or "").strip()
            if not uid or component.get("DTSTART") is None:
                continue
            starts_at = self._ical_datetime(component.decoded("DTSTART"), default_tz=default_tz)
            if component.get("DTEND") is not None:
                ends_at = self._ical_datetime(component.decoded("DTEND"), default_tz=default_tz)
            elif component.get("DURATION") is not None:
                ends_at = starts_at + component.decoded("DURATION")
            else:
                ends_at = starts_at + timedelta(hours=1)
            if ends_at <= starts_at:
                ends_at = starts_at + timedelta(hours=1)
            occurrence_id = f"{uid}:{starts_at.astimezone(UTC).isoformat()}"
            events.append(
                {
                    "uid": occurrence_id,
                    "summary": str(component.get("SUMMARY") or "Calendar event"),
                    "starts_at": starts_at,
                    "ends_at": ends_at,
                    "location": str(component.get("LOCATION") or "") or None,
                }
            )
        return events

    @staticmethod
    def _ical_datetime(value, *, default_tz: ZoneInfo) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=default_tz)
        return datetime.combine(value, time.min, tzinfo=default_tz)

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
