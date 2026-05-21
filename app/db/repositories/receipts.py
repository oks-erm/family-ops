from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PendingReceipt, Receipt, ReceiptItem, ReceiptStatus


class ReceiptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_extracted_receipt(
        self,
        *,
        user_id: UUID,
        household_id: UUID,
        shop_name: str | None,
        purchased_at: date | None,
        total_amount: str | None,
        currency: str | None,
        items: list[dict[str, Any]],
        raw_extraction: dict[str, Any],
    ) -> Receipt:
        receipt = Receipt(
            user_id=user_id,
            household_id=household_id,
            shop_name=shop_name,
            purchased_at=purchased_at,
            total_amount=total_amount,
            currency=currency,
            status=ReceiptStatus.extracted,
            raw_extraction=raw_extraction,
        )
        self.session.add(receipt)
        await self.session.flush()

        for item in items:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            self.session.add(
                ReceiptItem(
                    receipt_id=receipt.id,
                    name=name,
                    quantity=self._optional_string(item.get("quantity")),
                    total_amount=self._optional_string(item.get("total_amount")),
                )
            )

        await self.session.commit()
        await self.session.refresh(receipt)
        return receipt

    async def create_pending_receipt(
        self,
        *,
        user_id: UUID,
        household_id: UUID,
        telegram_chat_id: int,
        image_path: str,
        mime_type: str,
        extraction: dict[str, Any],
    ) -> PendingReceipt:
        pending_receipt = PendingReceipt(
            user_id=user_id,
            household_id=household_id,
            telegram_chat_id=telegram_chat_id,
            image_path=image_path,
            mime_type=mime_type,
            extraction=extraction,
        )
        self.session.add(pending_receipt)
        await self.session.commit()
        await self.session.refresh(pending_receipt)
        return pending_receipt

    async def get_pending_receipt(self, *, pending_receipt_id: UUID) -> PendingReceipt | None:
        return await self.session.get(PendingReceipt, pending_receipt_id)

    async def delete_pending_receipt(self, *, pending_receipt: PendingReceipt) -> None:
        await self.session.delete(pending_receipt)
        await self.session.commit()

    async def list_receipts_between(
        self,
        *,
        household_id: UUID,
        start_date: date,
        end_date: date,
    ) -> list[Receipt]:
        result = await self.session.execute(
            select(Receipt)
            .options(selectinload(Receipt.items))
            .where(
                Receipt.household_id == household_id,
                Receipt.status == ReceiptStatus.extracted,
            )
            .order_by(Receipt.purchased_at, Receipt.created_at)
        )
        receipts = list(result.scalars().all())
        return [
            receipt
            for receipt in receipts
            if start_date <= self.effective_receipt_date(receipt) <= end_date
        ]

    async def list_receipts_for_household(self, *, household_id: UUID) -> list[Receipt]:
        result = await self.session.execute(
            select(Receipt)
            .options(selectinload(Receipt.items))
            .where(
                Receipt.household_id == household_id,
                Receipt.status == ReceiptStatus.extracted,
            )
            .order_by(Receipt.created_at.desc())
        )
        return list(result.scalars().all())

    async def delete_receipt_for_household(self, *, receipt_id: UUID, household_id: UUID) -> bool:
        receipt = await self.session.get(Receipt, receipt_id)
        if receipt is None or receipt.household_id != household_id:
            return False

        await self.session.execute(delete(ReceiptItem).where(ReceiptItem.receipt_id == receipt_id))
        await self.session.delete(receipt)
        await self.session.commit()
        return True

    @staticmethod
    def effective_receipt_date(receipt: Receipt) -> date:
        if receipt.purchased_at is not None:
            return receipt.purchased_at
        return receipt.created_at.date()

    @staticmethod
    def amount_as_decimal(value: str | None) -> Decimal:
        if not value:
            return Decimal("0")
        cleaned = value.replace(",", ".").replace("€", "").strip()
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return Decimal("0")

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
