import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_telegram_user(
        self,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        first_name: str | None,
        last_name: str | None,
        username: str | None,
        timezone: str,
    ) -> User:
        stmt = (
            insert(User)
            .values(
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                first_name=first_name,
                last_name=last_name,
                username=username,
                timezone=timezone,
            )
            .on_conflict_do_update(
                index_elements=[User.telegram_user_id],
                set_={
                    "telegram_chat_id": telegram_chat_id,
                    "first_name": first_name,
                    "last_name": last_name,
                    "username": username,
                },
            )
            .returning(User)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one()

    async def list_users(self) -> list[User]:
        result = await self.session.execute(select(User).order_by(User.created_at))
        return list(result.scalars().all())

    async def get_by_id(self, *, user_id: UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_google_email(self, *, google_email: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.google_email == google_email.strip().casefold())
        )
        return result.scalar_one_or_none()

    async def create_scheduling_user(
        self,
        *,
        google_email: str,
        display_name: str,
        timezone: str,
        commit: bool = True,
    ) -> User:
        user = User(
            telegram_user_id=None,
            telegram_chat_id=None,
            first_name=display_name.strip()[:255] or "Tutor",
            last_name=None,
            username=None,
            timezone=timezone,
            google_email=google_email.strip().casefold(),
            family_dashboard_enabled=False,
        )
        self.session.add(user)
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        await self.session.refresh(user)
        return user

    async def create_dashboard_link_token(self, *, user_id: UUID) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(minutes=30)
        user = await self.session.get(User, user_id)
        if user is None:
            raise ValueError("User not found.")
        user.dashboard_link_token = token
        user.dashboard_link_expires_at = expires_at
        await self.session.commit()
        return token

    async def link_google_email_by_token(self, *, token: str, google_email: str) -> User | None:
        now = datetime.now(UTC)
        result = await self.session.execute(
            select(User).where(
                User.dashboard_link_token == token,
                User.dashboard_link_expires_at.is_not(None),
                User.dashboard_link_expires_at > now,
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            return None

        user.google_email = google_email.strip().casefold()
        user.dashboard_link_token = None
        user.dashboard_link_expires_at = None
        await self.session.commit()
        await self.session.refresh(user)
        return user
