from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
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
        feed = ICalFeed(
            user_id=user_id, household_id=household_id, name=name, url=url, is_active=True
        )
        self.session.add(feed)
        await self.session.commit()
        await self.session.refresh(feed)
        return feed

    async def list_active_ical_feeds(
        self,
        *,
        household_id: UUID | None = None,
        feed_id: UUID | None = None,
    ) -> list[ICalFeed]:
        query = select(ICalFeed).where(ICalFeed.is_active.is_(True))
        if household_id is not None:
            query = query.where(ICalFeed.household_id == household_id)
        if feed_id is not None:
            query = query.where(ICalFeed.id == feed_id)
        result = await self.session.execute(query.order_by(ICalFeed.created_at))
        return list(result.scalars().all())

    async def upsert_google_connection(
        self,
        *,
        user_id: UUID,
        household_id: UUID,
        account_email: str | None,
        external_account_id: str | None,
        access_token: str | None,
        refresh_token: str | None,
        token_expires_at: datetime | None,
        scopes: list[str],
    ) -> CalendarConnection:
        query = select(CalendarConnection).where(
            CalendarConnection.user_id == user_id,
            CalendarConnection.provider == CalendarProvider.google,
        )
        if account_email:
            query = query.where(CalendarConnection.account_email == account_email)
        result = await self.session.execute(query.order_by(CalendarConnection.created_at))
        connection = result.scalars().first()
        if connection is None and account_email:
            legacy_result = await self.session.execute(
                select(CalendarConnection)
                .where(
                    CalendarConnection.user_id == user_id,
                    CalendarConnection.provider == CalendarProvider.google,
                    CalendarConnection.account_email.is_(None),
                )
                .order_by(CalendarConnection.created_at)
            )
            connection = legacy_result.scalars().first()
        if connection is None:
            connection = CalendarConnection(
                user_id=user_id,
                household_id=household_id,
                provider=CalendarProvider.google,
                account_email=account_email,
                external_account_id=external_account_id,
                access_token=access_token,
                refresh_token=refresh_token,
                token_expires_at=token_expires_at,
                scopes=scopes,
            )
            self.session.add(connection)
        else:
            connection.household_id = household_id
            connection.account_email = account_email or connection.account_email
            connection.external_account_id = external_account_id
            connection.access_token = access_token or connection.access_token
            connection.refresh_token = refresh_token or connection.refresh_token
            connection.token_expires_at = token_expires_at
            connection.scopes = scopes
        await self.session.commit()
        await self.session.refresh(connection)
        return connection

    async def list_google_connections(
        self, *, household_id: UUID | None = None
    ) -> list[CalendarConnection]:
        query = select(CalendarConnection).where(
            CalendarConnection.provider == CalendarProvider.google
        )
        if household_id is not None:
            query = query.where(CalendarConnection.household_id == household_id)
        result = await self.session.execute(query.order_by(CalendarConnection.created_at))
        return list(result.scalars().all())

    async def first_google_connection(self, *, household_id: UUID) -> CalendarConnection | None:
        result = await self.session.execute(
            select(CalendarConnection)
            .where(
                CalendarConnection.household_id == household_id,
                CalendarConnection.provider == CalendarProvider.google,
            )
            .order_by(CalendarConnection.created_at)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def update_google_calendar_id_for_household(
        self,
        *,
        household_id: UUID,
        calendar_id: str,
    ) -> int:
        connections = await self.list_google_connections(household_id=household_id)
        for connection in connections:
            connection.external_account_id = calendar_id
        if connections:
            await self.session.commit()
        return len(connections)

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
        commit: bool = True,
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
        if commit:
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

    async def delete_cached_event(self, *, event: CalendarEventCache) -> None:
        await self.session.delete(event)
        await self.session.commit()

    async def delete_cached_event_by_external_id(
        self,
        *,
        source_id: UUID,
        external_event_id: str,
    ) -> None:
        await self.session.execute(
            delete(CalendarEventCache).where(
                CalendarEventCache.source_id == source_id,
                CalendarEventCache.external_event_id == external_event_id,
            )
        )
        await self.session.commit()

    async def remove_missing_google_events(
        self,
        *,
        source_id: UUID,
        calendar_id: str,
        seen_external_ids: set[str],
        starts_before: datetime,
        ends_after: datetime,
    ) -> int:
        query = delete(CalendarEventCache).where(
            CalendarEventCache.source_type == CalendarProvider.google,
            CalendarEventCache.source_id == source_id,
            CalendarEventCache.raw_event["_calendar_id"].astext == calendar_id,
            CalendarEventCache.starts_at < starts_before,
            CalendarEventCache.ends_at > ends_after,
        )
        if seen_external_ids:
            query = query.where(CalendarEventCache.external_event_id.not_in(seen_external_ids))
        result = await self.session.execute(query)
        await self.session.commit()
        return int(result.rowcount or 0)

    async def remove_legacy_google_events(
        self,
        *,
        source_id: UUID,
        starts_before: datetime,
        ends_after: datetime,
    ) -> int:
        result = await self.session.execute(
            delete(CalendarEventCache).where(
                CalendarEventCache.source_type == CalendarProvider.google,
                CalendarEventCache.source_id == source_id,
                CalendarEventCache.raw_event["_calendar_id"].astext.is_(None),
                CalendarEventCache.starts_at < starts_before,
                CalendarEventCache.ends_at > ends_after,
            )
        )
        await self.session.commit()
        return int(result.rowcount or 0)

    async def remove_missing_ical_events(
        self,
        *,
        source_id: UUID,
        seen_external_ids: set[str],
        starts_before: datetime,
        ends_after: datetime,
    ) -> int:
        query = delete(CalendarEventCache).where(
            CalendarEventCache.source_type == CalendarProvider.ical,
            CalendarEventCache.source_id == source_id,
            CalendarEventCache.starts_at < starts_before,
            CalendarEventCache.ends_at > ends_after,
        )
        if seen_external_ids:
            query = query.where(CalendarEventCache.external_event_id.not_in(seen_external_ids))
        result = await self.session.execute(query)
        await self.session.commit()
        return int(result.rowcount or 0)
