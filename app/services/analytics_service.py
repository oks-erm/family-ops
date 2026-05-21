from collections import defaultdict
import calendar
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.receipts import ReceiptRepository


class AnalyticsService:
    def __init__(self, session: AsyncSession | None = None) -> None:
        self.session = session

    def completion_percentage(self, *, completed: int, total: int) -> float:
        if total == 0:
            return 0.0
        return round((completed / total) * 100, 2)

    async def grocery_spend_summary(
        self,
        *,
        household_id: UUID,
        today: date,
        period: str,
        store_name: str | None = None,
        through_end_of_period: bool = False,
    ) -> str:
        if self.session is None:
            raise RuntimeError("A database session is required for spend summaries.")

        start_date, end_date = self._date_range(
            today=today,
            period=period,
            through_end_of_period=through_end_of_period,
        )
        receipt_repository = ReceiptRepository(self.session)
        receipts = await receipt_repository.list_receipts_between(
            household_id=household_id,
            start_date=start_date,
            end_date=end_date,
        )

        if store_name:
            normalized_store = store_name.lower()
            receipts = [
                receipt
                for receipt in receipts
                if receipt.shop_name and normalized_store in receipt.shop_name.lower()
            ]

        total = sum(
            (receipt_repository.amount_as_decimal(receipt.total_amount) for receipt in receipts),
            Decimal("0"),
        )
        by_store: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for receipt in receipts:
            store = receipt.shop_name or "Unknown"
            by_store[store] += receipt_repository.amount_as_decimal(receipt.total_amount)

        lines = [
            f"Groceries {period}: {self._money(total)}",
            f"Receipts: {len(receipts)}",
        ]
        if store_name:
            lines[0] = f"Groceries at {store_name} {period}: {self._money(total)}"
        if by_store:
            lines.append("By store:")
            for store, amount in sorted(by_store.items(), key=lambda item: item[1], reverse=True):
                lines.append(f"- {store}: {self._money(amount)}")
        return "\n".join(lines)

    @staticmethod
    def _date_range(
        *,
        today: date,
        period: str,
        through_end_of_period: bool = False,
    ) -> tuple[date, date]:
        if period == "this week":
            start_date = today - timedelta(days=today.weekday())
            end_date = start_date + timedelta(days=6) if through_end_of_period else today
            return start_date, end_date
        if period == "this month":
            start_date = today.replace(day=1)
            if through_end_of_period:
                last_day = calendar.monthrange(today.year, today.month)[1]
                return start_date, today.replace(day=last_day)
            return start_date, today
        return today.replace(day=1), today

    @staticmethod
    def _money(value: Decimal) -> str:
        return f"{value.quantize(Decimal('0.01'))} EUR"
