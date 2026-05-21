from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TransactionType
from app.db.repositories.activity import ActivityRepository
from app.db.repositories.finance import FinanceRepository
from app.db.repositories.prices import PriceRepository
from app.db.repositories.receipts import ReceiptRepository
from app.db.repositories.shopping import ShoppingRepository
from app.db.repositories.users import UserRepository
from app.services.recommendation_service import RecommendationService
from app.services.shopping_category_service import ShoppingCategoryService


class DashboardService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.receipt_repository = ReceiptRepository(session)
        self.finance_repository = FinanceRepository(session)
        self.activity_repository = ActivityRepository(session)
        self.price_repository = PriceRepository(session)
        self.shopping_repository = ShoppingRepository(session)
        self.user_repository = UserRepository(session)
        self.category_service = ShoppingCategoryService()

    async def summary(self, *, household_id: UUID, today: date) -> dict[str, object]:
        receipts = await self.receipt_repository.list_receipts_for_household(household_id=household_id)
        pending_items = await self.shopping_repository.list_all_pending_for_household(
            household_id=household_id,
        )
        month_start = today.replace(day=1)
        week_start = today - timedelta(days=today.weekday())

        month_receipts = [r for r in receipts if self.receipt_repository.effective_receipt_date(r) >= month_start]
        week_receipts = [r for r in receipts if self.receipt_repository.effective_receipt_date(r) >= week_start]
        series_start = self._shift_month(month_start, -5)
        series_transactions = await self.finance_repository.list_between(
            household_id=household_id,
            start_date=series_start,
            end_date=today,
        )
        month_transactions = [
            transaction
            for transaction in series_transactions
            if transaction.occurred_on >= month_start
        ]
        quotes = await self.price_repository.latest_for_household(household_id=household_id, limit=100)
        week_transactions = [
            transaction for transaction in month_transactions if transaction.occurred_on >= week_start
        ]

        month_total = self._sum_receipts(month_receipts)
        month_expense_total = month_total + self._sum_transactions(
            month_transactions,
            TransactionType.expense,
        )
        week_expense_total = self._sum_receipts(week_receipts) + self._sum_transactions(
            week_transactions,
            TransactionType.expense,
        )
        month_income_total = self._sum_transactions(month_transactions, TransactionType.income)
        next_month_projection, projection_note = self._next_month_projection(
            receipts=receipts,
            transactions=series_transactions,
            today=today,
        )

        return {
            "totals": {
                "this_week": self._money(week_expense_total),
                "this_month": self._money(month_expense_total),
                "income_month": self._money(month_income_total),
                "net_month": self._money(month_income_total - month_expense_total),
                "next_month_projection": self._money(next_month_projection),
                "projection_note": projection_note,
                "receipt_count_month": len(month_receipts),
                "pending_items": len(pending_items),
            },
            "expense_categories": self._expense_by_category(month_receipts, month_transactions),
            "shopping": self._pending_by_category(pending_items, quotes),
            "promotions": self._promotions_for_bought_items(receipts=receipts, quotes=quotes),
            "monthly_cashflow": self._monthly_cashflow(
                receipts=receipts,
                transactions=series_transactions,
                start_month=series_start,
                today=today,
            ),
            "activity": await self._activity(household_id=household_id),
            "recent_receipts": self._recent_receipts(receipts),
            "recommendations": await RecommendationService(self.session).latest_for_dashboard(
                household_id=household_id
            ),
        }

    def _sum_receipts(self, receipts: list[object]) -> Decimal:
        return sum(
            (self.receipt_repository.amount_as_decimal(receipt.total_amount) for receipt in receipts),
            Decimal("0"),
        )

    def _by_store(self, receipts: list[object]) -> list[dict[str, str]]:
        totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for receipt in receipts:
            totals[receipt.shop_name or "Unknown"] += self.receipt_repository.amount_as_decimal(
                receipt.total_amount
            )
        return [
            {"label": store, "value": self._money(amount)}
            for store, amount in sorted(totals.items(), key=lambda item: item[1], reverse=True)
        ]

    def _expense_by_category(
        self,
        receipts: list[object],
        transactions: list[object],
    ) -> list[dict[str, str]]:
        totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        if receipts:
            totals["Food"] += self._sum_receipts(receipts)
        for transaction in transactions:
            if transaction.transaction_type != TransactionType.expense:
                continue
            totals[transaction.category or "Other"] += self.finance_repository.amount_as_decimal(
                transaction.amount
            )
        return [
            {"label": category, "value": self._money(amount)}
            for category, amount in sorted(totals.items(), key=lambda item: item[1], reverse=True)
        ]

    def _pending_by_category(self, pending_items: list[object], quotes: list[object]) -> list[dict[str, object]]:
        grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
        for item in pending_items:
            category = self.category_service.category_for(item.name)
            quote = self._best_quote_for_item(item, quotes)
            grouped[category].append(
                {
                    "id": str(item.id),
                    "name": item.name,
                    "store": item.store_name_raw or "anywhere",
                    "price": self._quote_price(quote),
                    "price_store": quote.store_name if quote else "",
                    "product_name": quote.product_name if quote and quote.product_name else "",
                    "old_price": self._quote_old_price(quote),
                    "is_promotion": "yes" if quote and quote.is_promotion else "no",
                    "url": quote.product_url if quote and quote.product_url else "",
                }
            )
        return [{"category": category, "items": items} for category, items in sorted(grouped.items())]

    def _promotions_for_bought_items(
        self,
        *,
        receipts: list[object],
        quotes: list[object],
    ) -> list[dict[str, str]]:
        bought_names = {
            self._normalise(receipt_item.name)
            for receipt in receipts
            for receipt_item in receipt.items
            if receipt_item.name
        }
        promotions: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()

        for quote in quotes:
            if not quote.is_promotion:
                continue
            quote_name = self._normalise(quote.item_name)
            product_name = self._normalise(quote.product_name)
            if not self._matches_bought_item(quote_name=quote_name, product_name=product_name, bought_names=bought_names):
                continue
            key = (quote_name, self._normalise(quote.store_name), quote.price or "")
            if key in seen:
                continue
            seen.add(key)
            promotions.append(
                {
                    "item_name": quote.item_name,
                    "store_name": quote.store_name,
                    "product_name": quote.product_name or quote.item_name,
                    "price": self._quote_price(quote),
                    "old_price": self._quote_old_price(quote),
                    "url": quote.product_url or "",
                }
            )

        return promotions[:6]

    def _recent_receipts(self, receipts: list[object]) -> list[dict[str, str]]:
        recent = receipts[:8]
        return [
            {
                "id": str(receipt.id),
                "shop": receipt.shop_name or "Unknown",
                "date": self.receipt_repository.effective_receipt_date(receipt).isoformat(),
                "total": self._money(self.receipt_repository.amount_as_decimal(receipt.total_amount)),
            }
            for receipt in recent
        ]

    async def _activity(self, *, household_id: UUID) -> list[dict[str, str]]:
        entries = await self.activity_repository.list_recent(household_id=household_id, limit=20)
        users = await self.user_repository.list_users()
        user_names = {
            user.id: user.username or user.first_name or str(user.telegram_user_id)
            for user in users
        }
        return [
            {
                "action": entry.action.value,
                "entity_type": entry.entity_type,
                "actor": user_names.get(entry.user_id, "system"),
                "summary": entry.summary,
                "date": entry.created_at.isoformat(),
            }
            for entry in entries
        ]

    async def _price_quotes(self, *, household_id: UUID) -> list[dict[str, str]]:
        quotes = await self.price_repository.latest_for_household(household_id=household_id, limit=20)
        return [
            {
                "item_name": quote.item_name,
                "store_name": quote.store_name,
                "product_name": quote.product_name or quote.item_name,
                "price": f"{quote.price} {quote.currency}" if quote.price else "unknown",
                "old_price": f"{quote.old_price} {quote.currency}" if quote.old_price else "",
                "url": quote.product_url or "",
                "is_promotion": "yes" if quote.is_promotion else "no",
            }
            for quote in quotes
        ]

    def _monthly_cashflow(
        self,
        *,
        receipts: list[object],
        transactions: list[object],
        start_month: date,
        today: date,
    ) -> list[dict[str, object]]:
        months = [self._shift_month(start_month, offset) for offset in range(6)]
        totals: dict[tuple[int, int], dict[str, Decimal]] = {
            (month.year, month.month): {"income": Decimal("0"), "expenses": Decimal("0")}
            for month in months
        }

        for receipt in receipts:
            receipt_date = self.receipt_repository.effective_receipt_date(receipt)
            key = (receipt_date.year, receipt_date.month)
            if key in totals and receipt_date <= today:
                totals[key]["expenses"] += self.receipt_repository.amount_as_decimal(receipt.total_amount)

        for transaction in transactions:
            key = (transaction.occurred_on.year, transaction.occurred_on.month)
            if key not in totals:
                continue
            amount = self.finance_repository.amount_as_decimal(transaction.amount)
            if transaction.transaction_type == TransactionType.income:
                totals[key]["income"] += amount
            elif transaction.transaction_type == TransactionType.expense:
                totals[key]["expenses"] += amount

        series = [
            {
                "label": month.strftime("%b"),
                "month": month.strftime("%Y-%m"),
                "income": self._money(totals[(month.year, month.month)]["income"]),
                "expenses": self._money(totals[(month.year, month.month)]["expenses"]),
                "income_value": float(totals[(month.year, month.month)]["income"]),
                "expenses_value": float(totals[(month.year, month.month)]["expenses"]),
            }
            for month in months
        ]
        first_data_index = next(
            (
                index
                for index, row in enumerate(series)
                if row["income_value"] or row["expenses_value"]
            ),
            len(series) - 1,
        )
        return series[first_data_index:]

    def _next_month_projection(
        self,
        *,
        receipts: list[object],
        transactions: list[object],
        today: date,
    ) -> tuple[Decimal, str]:
        current_month = (today.year, today.month)
        complete_month_totals: dict[tuple[int, int], Decimal] = defaultdict(lambda: Decimal("0"))
        current_month_total = Decimal("0")

        for receipt in receipts:
            receipt_date = self.receipt_repository.effective_receipt_date(receipt)
            amount = self.receipt_repository.amount_as_decimal(receipt.total_amount)
            receipt_month = (receipt_date.year, receipt_date.month)
            if receipt_month < current_month:
                complete_month_totals[receipt_month] += amount
            elif receipt_month == current_month:
                current_month_total += amount

        for transaction in transactions:
            if transaction.transaction_type != TransactionType.expense:
                continue
            if self._normalise(transaction.category) == "taxes":
                continue
            amount = self.finance_repository.amount_as_decimal(transaction.amount)
            transaction_month = (transaction.occurred_on.year, transaction.occurred_on.month)
            if transaction_month < current_month:
                complete_month_totals[transaction_month] += amount
            elif transaction_month == current_month:
                current_month_total += amount

        recent_complete_months = sorted(complete_month_totals.items())[-3:]
        if recent_complete_months:
            total = sum((amount for _, amount in recent_complete_months), Decimal("0"))
            count = Decimal(len(recent_complete_months))
            return total / count, f"average of last {len(recent_complete_months)} complete month(s)"

        if current_month_total > 0 and today.day >= 7:
            days_this_month = Decimal(monthrange(today.year, today.month)[1])
            return current_month_total / Decimal(today.day) * days_this_month, "current month run rate"

        if current_month_total > 0:
            return current_month_total, "current month receipts so far"

        return Decimal("0"), "no receipt history yet"

    def _sum_transactions(
        self,
        transactions: list[object],
        transaction_type: TransactionType,
    ) -> Decimal:
        return sum(
            (
                self.finance_repository.amount_as_decimal(transaction.amount)
                for transaction in transactions
                if transaction.transaction_type == transaction_type
            ),
            Decimal("0"),
        )

    @staticmethod
    def _best_quote_for_item(item: object, quotes: list[object]) -> object | None:
        item_name = DashboardService._normalise(item.name)
        item_store = DashboardService._normalise(item.store_name_raw or "")
        candidates = []

        for quote in quotes:
            if DashboardService._normalise(quote.item_name) != item_name:
                continue
            if not DashboardService._valid_quote_for_item(item_store=item_store, quote=quote):
                continue
            if item_store and item_store != "anywhere" and DashboardService._normalise(quote.store_name) != item_store:
                continue
            price = DashboardService._decimal_price(quote.price)
            if price is None:
                continue
            candidates.append((price, DashboardService._store_rank(quote.store_name), quote))

        if not candidates:
            return None
        return sorted(candidates, key=lambda candidate: (candidate[0], candidate[1]))[0][2]

    @staticmethod
    def _valid_quote_for_item(*, item_store: str, quote: object) -> bool:
        store = DashboardService._normalise(quote.store_name)
        product_name = DashboardService._normalise(quote.product_name)
        if "pesquisou por" in product_name or "search" in product_name:
            return False
        if store == "minimix" and item_store not in {"minimix", "mini mix"}:
            return False
        return True

    @staticmethod
    def _decimal_price(value: str | None) -> Decimal | None:
        if not value:
            return None
        try:
            return Decimal(value.replace(",", ".").replace("€", "").strip())
        except InvalidOperation:
            return None

    @staticmethod
    def _store_rank(store_name: str | None) -> int:
        store = DashboardService._normalise(store_name)
        ranks = {
            "lidl": 0,
            "continente": 1,
            "aldi": 2,
            "pingo doce": 3,
            "mercadona": 4,
            "minimix": 9,
        }
        return ranks.get(store, 5)

    @staticmethod
    def _quote_price(quote: object | None) -> str:
        if quote is None or not quote.price:
            return ""
        return f"{quote.price} {quote.currency}"

    @staticmethod
    def _quote_old_price(quote: object | None) -> str:
        if quote is None or not quote.old_price:
            return ""
        return f"{quote.old_price} {quote.currency}"

    @staticmethod
    def _normalise(value: str | None) -> str:
        return (value or "").strip().casefold()

    @staticmethod
    def _matches_bought_item(
        *,
        quote_name: str,
        product_name: str,
        bought_names: set[str],
    ) -> bool:
        if not quote_name:
            return False
        for bought_name in bought_names:
            if not bought_name:
                continue
            if quote_name in bought_name or bought_name in quote_name:
                return True
            if product_name and bought_name in product_name:
                return True
        return False

    @staticmethod
    def _shift_month(value: date, offset: int) -> date:
        month_index = value.year * 12 + value.month - 1 + offset
        year = month_index // 12
        month = month_index % 12 + 1
        return date(year, month, 1)

    @staticmethod
    def _money(value: Decimal) -> str:
        return f"{value.quantize(Decimal('0.01'))} EUR"
