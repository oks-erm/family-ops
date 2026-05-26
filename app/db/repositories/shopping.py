from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ShoppingItem, ShoppingItemStatus
from app.services.grocery_normalizer import GroceryNormalizer


class ShoppingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.normalizer = GroceryNormalizer()

    async def add_item(
        self,
        *,
        user_id: UUID,
        household_id: UUID,
        name: str,
        store_name: str | None,
    ) -> ShoppingItem:
        item = ShoppingItem(
            user_id=user_id,
            household_id=household_id,
            name=name,
            store_name_raw=store_name,
            status=ShoppingItemStatus.pending,
        )
        self.session.add(item)
        await self.session.commit()
        await self.session.refresh(item)
        return item

    async def list_pending_for_store(
        self,
        *,
        household_id: UUID,
        store_name: str,
    ) -> list[ShoppingItem]:
        normalized_store = store_name.strip().lower()
        result = await self.session.execute(
            select(ShoppingItem)
            .where(
                ShoppingItem.household_id == household_id,
                ShoppingItem.status == ShoppingItemStatus.pending,
                ShoppingItem.store_name_raw.ilike(normalized_store),
            )
            .order_by(ShoppingItem.created_at)
        )
        return list(result.scalars().all())

    async def list_all_pending_for_household(self, *, household_id: UUID) -> list[ShoppingItem]:
        return await self._list_all_pending(household_id=household_id)

    async def mark_pending_items_purchased_by_names(
        self,
        *,
        household_id: UUID,
        item_names: list[str],
    ) -> list[ShoppingItem]:
        pending_items = await self._list_all_pending(household_id=household_id)
        normalized_targets = [self._normalize_name(name) for name in item_names if name.strip()]
        matched: list[ShoppingItem] = []

        for item in pending_items:
            item_name = self.normalizer.normalize(item.name)
            if any(self._names_match(item_name, target) for target in normalized_targets):
                item.status = ShoppingItemStatus.purchased
                matched.append(item)

        if matched:
            await self.session.commit()
            for item in matched:
                await self.session.refresh(item)
        return matched

    async def remove_pending_items_by_names(
        self,
        *,
        household_id: UUID,
        item_names: list[str],
    ) -> list[ShoppingItem]:
        pending_items = await self._list_all_pending(household_id=household_id)
        normalized_targets = [self._normalize_name(name) for name in item_names if name.strip()]
        matched: list[ShoppingItem] = []

        for item in pending_items:
            item_name = self.normalizer.normalize(item.name)
            if any(self._names_match(item_name, target) for target in normalized_targets):
                item.status = ShoppingItemStatus.skipped
                matched.append(item)

        if matched:
            await self.session.commit()
            for item in matched:
                await self.session.refresh(item)
        return matched

    async def reassign_store(
        self,
        *,
        household_id: UUID,
        item_name: str,
        new_store: str | None,
    ) -> ShoppingItem | None:
        pending_items = await self._list_all_pending(household_id=household_id)
        normalized_target = self._normalize_name(item_name)
        matched = next(
            (i for i in pending_items if self._names_match(self.normalizer.normalize(i.name), normalized_target)),
            None,
        )
        if matched is None:
            return None
        matched.store_name_raw = new_store
        await self.session.commit()
        await self.session.refresh(matched)
        return matched

    async def remove_pending_item_by_id(
        self,
        *,
        household_id: UUID,
        item_id: UUID,
    ) -> ShoppingItem | None:
        item = await self.session.get(ShoppingItem, item_id)
        if item is None or item.household_id != household_id:
            return None
        if item.status != ShoppingItemStatus.pending:
            return None
        item.status = ShoppingItemStatus.skipped
        await self.session.commit()
        await self.session.refresh(item)
        return item

    async def _list_all_pending(self, *, household_id: UUID) -> list[ShoppingItem]:
        result = await self.session.execute(
            select(ShoppingItem)
            .where(
                ShoppingItem.household_id == household_id,
                ShoppingItem.status == ShoppingItemStatus.pending,
            )
            .order_by(ShoppingItem.created_at)
        )
        return list(result.scalars().all())

    def _normalize_name(self, value: str) -> str:
        return self.normalizer.normalize(value)

    def _names_match(self, item_name: str, target_name: str) -> bool:
        if item_name == target_name or item_name in target_name or target_name in item_name:
            return True
        item_tokens = set(item_name.split())
        target_tokens = set(target_name.split())
        return bool(item_tokens and target_tokens and item_tokens.issubset(target_tokens))
