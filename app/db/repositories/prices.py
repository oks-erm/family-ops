from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ShoppingPriceQuote


class PriceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_quote(
        self,
        *,
        household_id: UUID,
        shopping_item_id: UUID | None,
        item_name: str,
        store_name: str,
        product_name: str | None,
        price: str | None,
        old_price: str | None,
        product_url: str | None,
        is_promotion: bool,
        source: str = "web",
        commit: bool = True,
    ) -> ShoppingPriceQuote:
        quote = ShoppingPriceQuote(
            household_id=household_id,
            shopping_item_id=shopping_item_id,
            item_name=item_name,
            store_name=store_name,
            product_name=product_name,
            price=price,
            old_price=old_price,
            currency="EUR",
            product_url=product_url,
            is_promotion=is_promotion,
            fetched_at=datetime.now(UTC),
            source=source,
        )
        self.session.add(quote)
        if commit:
            await self.session.commit()
            await self.session.refresh(quote)
        return quote

    async def latest_for_household(self, *, household_id: UUID, limit: int = 50) -> list[ShoppingPriceQuote]:
        result = await self.session.execute(
            select(ShoppingPriceQuote)
            .where(ShoppingPriceQuote.household_id == household_id)
            .order_by(ShoppingPriceQuote.fetched_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
