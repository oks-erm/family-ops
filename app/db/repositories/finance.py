from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FinancialTransaction, TransactionType


class FinanceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_transaction(
        self,
        *,
        user_id: UUID,
        household_id: UUID,
        transaction_type: TransactionType,
        category: str,
        merchant: str | None,
        description: str,
        amount: str,
        currency: str,
        occurred_on: date,
        source: str,
        raw_data: dict[str, object] | None = None,
        commit: bool = True,
    ) -> FinancialTransaction:
        transaction = FinancialTransaction(
            user_id=user_id,
            household_id=household_id,
            transaction_type=transaction_type,
            category=category,
            merchant=merchant,
            description=description,
            amount=amount,
            currency=currency,
            occurred_on=occurred_on,
            source=source,
            raw_data=raw_data or {},
        )
        self.session.add(transaction)
        if commit:
            await self.session.commit()
            await self.session.refresh(transaction)
        return transaction

    async def list_between(
        self,
        *,
        household_id: UUID,
        start_date: date,
        end_date: date,
    ) -> list[FinancialTransaction]:
        result = await self.session.execute(
            select(FinancialTransaction)
            .where(
                FinancialTransaction.household_id == household_id,
                FinancialTransaction.occurred_on >= start_date,
                FinancialTransaction.occurred_on <= end_date,
            )
            .order_by(FinancialTransaction.occurred_on.desc(), FinancialTransaction.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    def amount_as_decimal(value: str | None) -> Decimal:
        if not value:
            return Decimal("0")
        cleaned = value.replace(",", ".").replace("€", "").strip()
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return Decimal("0")
