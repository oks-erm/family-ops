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

    async def finance_summary(
        self,
        *,
        household_id: UUID,
        today: date,
        period: str,
        query_kind: str = "spend",
        category_filter: str | None = None,
    ) -> str:
        from app.db.repositories.finance import FinanceRepository
        from app.db.models import TransactionType

        if self.session is None:
            raise RuntimeError("A database session is required for finance summaries.")

        start_date, end_date = self._date_range(today=today, period=period)
        period_label = self._period_label(period=period, start=start_date, end=end_date)

        finance_repo = FinanceRepository(self.session)
        transactions = await finance_repo.list_between(
            household_id=household_id,
            start_date=start_date,
            end_date=end_date,
        )

        if query_kind == "income":
            typed = [t for t in transactions if t.transaction_type == TransactionType.income]
        else:
            typed = [t for t in transactions if t.transaction_type == TransactionType.expense]

        filtered = typed
        if category_filter:
            cf = category_filter.lower()
            filtered = [
                t for t in typed
                if cf in (t.category or "").lower() or cf in (t.description or "").lower()
            ]

        # If category search yielded nothing, try receipt item search
        if category_filter and not filtered and query_kind == "spend":
            return await self._item_spend_summary(
                household_id=household_id,
                start_date=start_date,
                end_date=end_date,
                period_label=period_label,
                item_name=category_filter,
            )

        total = sum(
            (finance_repo.amount_as_decimal(t.amount) for t in filtered),
            Decimal("0"),
        )
        kind_label = "Income" if query_kind == "income" else "Spending"
        if category_filter:
            header = f"{kind_label} on '{category_filter}' {period_label}: {self._money(total)}"
        else:
            header = f"{kind_label} {period_label}: {self._money(total)}"

        lines = [header, f"Transactions: {len(filtered)}"]
        if not category_filter:
            by_cat: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
            for t in filtered:
                by_cat[t.category or "Other"] += finance_repo.amount_as_decimal(t.amount)
            if by_cat:
                lines.append("By category:")
                for cat, amt in sorted(by_cat.items(), key=lambda x: x[1], reverse=True):
                    lines.append(f"- {cat}: {self._money(amt)}")
        else:
            for t in sorted(filtered, key=lambda x: x.occurred_on, reverse=True)[:10]:
                lines.append(
                    f"- {t.occurred_on.strftime('%d %b')}: {t.description} "
                    f"{self._money(finance_repo.amount_as_decimal(t.amount))}"
                )
            if len(filtered) > 10:
                lines.append(f"...and {len(filtered) - 10} more.")
        return "\n".join(lines)

    async def _item_spend_summary(
        self,
        *,
        household_id: UUID,
        start_date: date,
        end_date: date,
        period_label: str,
        item_name: str,
    ) -> str:
        if self.session is None:
            raise RuntimeError("A database session is required.")

        receipt_repo = ReceiptRepository(self.session)
        receipts = await receipt_repo.list_receipts_between(
            household_id=household_id,
            start_date=start_date,
            end_date=end_date,
        )

        item_lower = item_name.lower()
        matches: list[tuple[object, object]] = []
        for receipt in receipts:
            for item in receipt.items:
                if item_lower in (item.name or "").lower():
                    matches.append((receipt, item))

        if not matches:
            return f"No records found for '{item_name}' {period_label}."

        total = sum(
            (receipt_repo.amount_as_decimal(item.total_amount) for _, item in matches),
            Decimal("0"),
        )
        lines = [
            f"Spending on '{item_name}' {period_label}: {self._money(total)}",
            f"Found in {len(matches)} receipt line(s).",
        ]
        for receipt, item in sorted(
            matches,
            key=lambda x: (getattr(x[0], "purchased_at", None) or date.min),
            reverse=True,
        )[:10]:
            dt = getattr(receipt, "purchased_at", None) or date.min
            store = getattr(receipt, "shop_name", None) or "Unknown"
            lines.append(
                f"- {dt.strftime('%d %b')} {store}: {item.name} "
                f"{self._money(receipt_repo.amount_as_decimal(item.total_amount))}"
            )
        if len(matches) > 10:
            lines.append(f"...and {len(matches) - 10} more.")
        return "\n".join(lines)

    @staticmethod
    def _period_label(*, period: str, start: date, end: date) -> str:
        _M = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        if period in {"this month", "this week", "this year", "last month", "last year"}:
            return period
        if period.startswith("month:"):
            _, _, ym = period.partition(":")
            return f"{_M[int(ym[5:7])]} {ym[:4]}"
        if period.startswith("year:"):
            return f"in {period[5:]}"
        return period

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
        if period == "last month":
            first_this = today.replace(day=1)
            end_last = first_this - timedelta(days=1)
            return end_last.replace(day=1), end_last
        if period == "this year":
            return today.replace(month=1, day=1), today
        if period == "last year":
            y = today.year - 1
            return date(y, 1, 1), date(y, 12, 31)
        if period.startswith("month:"):
            _, _, ym = period.partition(":")
            year, month = int(ym[:4]), int(ym[5:7])
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        if period.startswith("year:"):
            y = int(period[5:])
            return date(y, 1, 1), date(y, 12, 31)
        return today.replace(day=1), today

    @staticmethod
    def _money(value: Decimal) -> str:
        return f"{value.quantize(Decimal('0.01'))} EUR"
