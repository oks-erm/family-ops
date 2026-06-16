from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CalendarConnection, CalendarEventCache, CalendarProvider, ICalFeed


class CalendarRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_ical_feed(
        self,
        *,
        user_id: UUID,
        household_id: UUID,
        name: str,
        url: str,
    ) -> ICalFeed:
        feed = ICalFeed(user_id=user_id, household_id=household_id, name=name, url=url, is_active=True)
        self.session.add(feed)
        await self.session.commit()
        await self.session.refresh(feed)
        return feed

    async def list_active_ical_feeds(self, *, household_id: UUID | None = None) -> list[ICalFeed]:
        query = select(ICalFeed).where(ICalFeed.is_active.is_(True))
        if household_id is not None:
            query = query.where(ICalFeed.household_id == household_id)
        result = await self.session.execute(query.order_by(ICalFeed.created_at))
        return list(result.scalars().all())

    async def upsert_google_connection(
        self,
        *,
        user_id: UUID,
        household_id: UUID,
        external_account_id: str | None,
        access_token: str | None,
        refresh_token: str | None,
        token_expires_at: datetime | None,
        scopes: list[str],
    ) -> CalendarConnection:
        result = await self.session.execute(
            select(CalendarConnection).where(
                CalendarConnection.user_id == user_id,
                CalendarConnection.provider == CalendarProvider.google,
                CalendarConnection.external_account_id == external_account_id,
            )
        )
        connection = result.scalars().first()
        if connection is None:
            connection = CalendarConnection(
                user_id=user_id,
                household_id=household_id,
                provider=CalendarProvider.google,
                external_account_id=external_account_id,
                access_token=access_token,
                refresh_token=refresh_token,
                token_expires_at=token_expires_at,
                scopes=scopes,
            )
            self.session.add(connection)
        else:
            connection.household_id = household_id
            connection.access_token = access_token or connection.access_token
            connection.refresh_token = refresh_token or connection.refresh_token
            connection.token_expires_at = token_expires_at
            connection.scopes = scopes
        await self.session.commit()
        await self.session.refresh(connection)
        return connection

    async def list_google_connections(self, *, household_id: UUID | None = None) -> list[CalendarConnection]:
        query = select(CalendarConnection).where(CalendarConnection.provider == CalendarProvider.google)
        if household_id is not None:
            query = query.where(CalendarConnection.household_id == household_id)
        result = await self.session.execute(query.order_by(CalendarConnection.created_at))
        return list(result.scalars().all())

    async def upsert_event(
        self,
        *,
        household_id: UUID,
        user_id: UUID | None,
        source_type: CalendarProvider,
        source_id: UUID,
        external_event_id: str,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        location: str | None,
        raw_event: dict[str, object],
    ) -> CalendarEventCache:
        stmt = (
            insert(CalendarEventCache)
            .values(
                household_id=household_id,
                user_id=user_id,
                source_type=source_type,
                source_id=source_id,
                external_event_id=external_event_id,
                title=title,
                starts_at=starts_at,
                ends_at=ends_at,
                location=location,
                raw_event=raw_event,
            )
            .on_conflict_do_update(
                constraint="uq_calendar_event_source",
                set_={
                    "household_id": household_id,
                    "user_id": user_id,
                    "title": title,
                    "starts_at": starts_at,
                    "ends_at": ends_at,
                    "location": location,
                    "raw_event": raw_event,
                },
            )
            .returning(CalendarEventCache)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one()

    async def list_events_between(
        self,
        *,
        household_id: UUID,
        starts_before: datetime,
        ends_after: datetime,
    ) -> list[CalendarEventCache]:
        result = await self.session.execute(
            select(CalendarEventCache)
            .where(
                CalendarEventCache.household_id == household_id,
                CalendarEventCache.starts_at < starts_before,
                CalendarEventCache.ends_at > ends_after,
            )
            .order_by(CalendarEventCache.starts_at)
        )
        return list(result.scalars().all())
