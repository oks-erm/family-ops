import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Household,
    HouseholdMember,
    HouseholdRole,
    User,
)


class HouseholdRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_household_for_user(self, *, user_id: UUID) -> Household | None:
        result = await self.session.execute(
            select(Household)
            .join(HouseholdMember, HouseholdMember.household_id == Household.id)
            .where(HouseholdMember.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def ensure_household_for_user(
        self, *, user: User, commit: bool = True
    ) -> Household:
        household = await self.get_household_for_user(user_id=user.id)
        if household is not None:
            return household

        household = Household(
            name=f"{user.first_name or 'Household'}'s household",
            invite_code=await self._new_invite_code(),
        )
        self.session.add(household)
        await self.session.flush()
        self.session.add(
            HouseholdMember(
                household_id=household.id,
                user_id=user.id,
                role=HouseholdRole.owner,
            )
        )
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        await self.session.refresh(household)
        return household

    async def join_by_invite_code(self, *, user: User, invite_code: str) -> Household | None:
        result = await self.session.execute(
            select(Household).where(Household.invite_code == invite_code.strip().upper())
        )
        household = result.scalar_one_or_none()
        if household is None:
            return None

        result = await self.session.execute(
            select(HouseholdMember).where(HouseholdMember.user_id == user.id)
        )
        membership = result.scalar_one_or_none()
        if membership is None:
            self.session.add(
                HouseholdMember(
                    household_id=household.id,
                    user_id=user.id,
                    role=HouseholdRole.member,
                )
            )
        else:
            membership.household_id = household.id
            membership.role = HouseholdRole.member

        # Joining changes membership only. Existing records retain their original household.
        # Transfers require a separate, explicitly reviewed operation; never infer one from /join.
        await self.session.commit()
        await self.session.refresh(household)
        return household

    async def update_google_calendar_id(self, *, household: Household, calendar_id: str | None) -> Household:
        household.google_calendar_id = calendar_id
        await self.session.commit()
        await self.session.refresh(household)
        return household

    async def _new_invite_code(self) -> str:
        while True:
            code = secrets.token_urlsafe(6).replace("-", "").replace("_", "").upper()[:8]
            exists = await self.session.scalar(select(Household.id).where(Household.invite_code == code))
            if not exists:
                return code
