from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import ActivityAction
from app.db.repositories.activity import ActivityRepository
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.receipts import ReceiptRepository
from app.db.repositories.shopping import ShoppingRepository
from app.db.repositories.users import UserRepository
from app.services.ai_router import AiRouter


class ReceiptService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.receipt_repository = ReceiptRepository(session)
        self.shopping_repository = ShoppingRepository(session)
        self.household_repository = HouseholdRepository(session)
        self.activity_repository = ActivityRepository(session)
        self.ai_router = AiRouter(settings)

    async def extract_and_create_pending(
        self,
        *,
        user_id: UUID,
        telegram_chat_id: int,
        image_bytes: bytes,
        image_path: str,
        mime_type: str,
    ) -> tuple[str, str | None]:
        extraction = await self.ai_router.extract_receipt(
            image_bytes=image_bytes,
            mime_type=mime_type,
        )
        if extraction.data is None:
            return "I could not extract this receipt.", None

        data = extraction.data
        user = await UserRepository(self.household_repository.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before receipts can be processed.")
        household = await self.household_repository.ensure_household_for_user(user=user)
        pending_receipt = await self.receipt_repository.create_pending_receipt(
            user_id=user_id,
            household_id=household.id,
            telegram_chat_id=telegram_chat_id,
            image_path=image_path,
            mime_type=mime_type,
            extraction=data,
        )
        return self._preview_text(data), str(pending_receipt.id)

    async def confirm_pending_receipt(self, *, pending_receipt_id: UUID) -> str:
        pending_receipt = await self.receipt_repository.get_pending_receipt(
            pending_receipt_id=pending_receipt_id
        )
        if pending_receipt is None:
            return "This receipt confirmation is no longer available."

        data = pending_receipt.extraction
        items = data.get("items") if isinstance(data.get("items"), list) else []
        item_names = [str(item.get("name")) for item in items if isinstance(item, dict) and item.get("name")]

        receipt = await self.receipt_repository.create_extracted_receipt(
            user_id=pending_receipt.user_id,
            household_id=pending_receipt.household_id,
            shop_name=self._optional_string(data.get("shop_name")),
            purchased_at=self._parse_date(data.get("purchased_at")) or pending_receipt.created_at.date(),
            total_amount=self._optional_string(data.get("total_amount")),
            currency=self._optional_string(data.get("currency")),
            items=[item for item in items if isinstance(item, dict)],
            raw_extraction=data,
        )
        matched_items = await self.shopping_repository.mark_pending_items_purchased_by_names(
            household_id=pending_receipt.household_id,
            item_names=item_names,
        )
        await self.activity_repository.log(
            household_id=pending_receipt.household_id,
            user_id=pending_receipt.user_id,
            action=ActivityAction.created,
            entity_type="receipt",
            entity_id=receipt.id,
            summary=f"Saved receipt: {receipt.shop_name or 'Unknown'} {receipt.total_amount or ''} {receipt.currency or ''}".strip(),
            commit=False,
        )
        image_path = Path(pending_receipt.image_path)
        await self.receipt_repository.delete_pending_receipt(pending_receipt=pending_receipt)
        if image_path.exists():
            image_path.unlink()

        cleared_names = [item.name for item in matched_items]
        return self._summary_text(
            shop_name=receipt.shop_name,
            total_amount=receipt.total_amount,
            currency=receipt.currency,
            extracted_count=len(item_names),
            cleared_names=cleared_names,
        )

    async def discard_pending_receipt(self, *, pending_receipt_id: UUID) -> str:
        pending_receipt = await self.receipt_repository.get_pending_receipt(
            pending_receipt_id=pending_receipt_id
        )
        if pending_receipt is None:
            return "This receipt confirmation is no longer available."

        image_path = Path(pending_receipt.image_path)
        await self.receipt_repository.delete_pending_receipt(pending_receipt=pending_receipt)
        if image_path.exists():
            image_path.unlink()
        return "Receipt discarded."

    def _preview_text(self, data: dict[str, Any]) -> str:
        items = data.get("items") if isinstance(data.get("items"), list) else []
        lines = ["Receipt extracted. Confirm before I save it and clear shopping items."]
        shop_name = self._optional_string(data.get("shop_name"))
        total_amount = self._optional_string(data.get("total_amount"))
        currency = self._optional_string(data.get("currency"))
        if shop_name:
            lines.append(f"Shop: {shop_name}")
        if total_amount:
            total = f"{total_amount} {currency}" if currency else total_amount
            lines.append(f"Total: {total}")
        lines.append(f"Items found: {len(items)}")
        preview_names = [
            str(item.get("name")).strip()
            for item in items
            if isinstance(item, dict) and item.get("name")
        ]
        if preview_names:
            lines.extend(f"- {name}" for name in preview_names)
        return "\n".join(lines)

    @staticmethod
    def _summary_text(
        *,
        shop_name: str | None,
        total_amount: str | None,
        currency: str | None,
        extracted_count: int,
        cleared_names: list[str],
    ) -> str:
        lines = ["Receipt saved."]
        if shop_name:
            lines.append(f"Shop: {shop_name}")
        if total_amount:
            total = f"{total_amount} {currency}" if currency else total_amount
            lines.append(f"Total: {total}")
        lines.append(f"Extracted items: {extracted_count}")
        if cleared_names:
            lines.append("Cleared from shopping list: " + ", ".join(cleared_names))
        else:
            lines.append("No pending shopping list items matched the receipt.")
        return "\n".join(lines)

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None
