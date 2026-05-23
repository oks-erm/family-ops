from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TaskCompletion, TaskStatus, TransactionType
from app.db.repositories.activity import ActivityRepository
from app.db.repositories.finance import FinanceRepository
from app.db.repositories.prices import PriceRepository
from app.db.repositories.receipts import ReceiptRepository
from app.db.repositories.shopping import ShoppingRepository
from app.db.repositories.users import UserRepository
from app.services.finance_category_service import FinanceCategoryService
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

    async def summary(
        self,
        *,
        household_id: UUID,
        today: date,
        selected_month: date | None = None,
        period_start: date | None = None,
        period_end: date | None = None,
        period_label: str | None = None,
    ) -> dict[str, object]:
        receipts = await self.receipt_repository.list_receipts_for_household(household_id=household_id)
        pending_items = await self.shopping_repository.list_all_pending_for_household(
            household_id=household_id,
        )
        month_start = period_start or (selected_month or today).replace(day=1)
        month_end = period_end or date(
            month_start.year,
            month_start.month,
            monthrange(month_start.year, month_start.month)[1],
        )
        bounded_period_end = min(month_end, today) if month_start <= today else month_end
        week_start = today - timedelta(days=today.weekday())

        month_receipts = [
            r
            for r in receipts
            if month_start <= self.receipt_repository.effective_receipt_date(r) <= month_end
        ]
        week_receipts = [
            r
            for r in receipts
            if week_start <= self.receipt_repository.effective_receipt_date(r) <= today
        ]
        is_custom_period = period_start is not None or period_end is not None
        series_start = month_start if is_custom_period else self._shift_month(month_start, -5)
        series_end = month_end if is_custom_period else month_start
        all_transactions = await self.finance_repository.list_for_household(household_id=household_id)
        await self._recategorize_known_other_transactions(all_transactions)
        series_transactions = [
            transaction
            for transaction in all_transactions
            if series_start <= self._dashboard_transaction_date(transaction) <= bounded_period_end
        ]
        month_transactions = [
            transaction
            for transaction in series_transactions
            if month_start <= self._dashboard_transaction_date(transaction) <= month_end
        ]
        quotes = await self.price_repository.latest_for_household(household_id=household_id, limit=100)
        week_transactions = [
            transaction
            for transaction in month_transactions
            if self._dashboard_transaction_date(transaction) >= week_start
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
        saved_total = month_income_total - month_expense_total
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
                "saved_month": self._money(saved_total),
                "saved_month_value": float(saved_total),
                "next_month_projection": self._money(next_month_projection),
                "projection_note": projection_note,
                "receipt_count_month": len(month_receipts),
                "pending_items": len(pending_items),
            },
            "period": {
                "month": month_start.strftime("%Y-%m"),
                "start": month_start.isoformat(),
                "end": month_end.isoformat(),
                "label": period_label or month_start.strftime("%B %Y"),
                "is_current_month": month_start == today.replace(day=1) and month_end.month == today.month,
            },
            "expense_categories": self._expense_by_category(month_receipts, month_transactions),
            "category_details": self._category_details(month_receipts, month_transactions),
            "transactions": self._recent_transactions(month_transactions),
            "finance_categories": FinanceCategoryService.categories(),
            "task_stats": await self._task_stats(household_id=household_id, start=month_start, end=month_end),
            "shopping": self._pending_by_category(pending_items, quotes),
            "promotions": self._promotions_for_household_items(
                receipts=receipts,
                pending_items=pending_items,
                quotes=quotes,
            ),
            "monthly_cashflow": self._monthly_cashflow(
                receipts=receipts,
                transactions=series_transactions,
                start_month=series_start,
                end_month=series_end,
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

    async def _recategorize_known_other_transactions(self, transactions: list[object]) -> None:
        category_service = FinanceCategoryService()
        changed = False
        for transaction in transactions:
            if transaction.transaction_type != TransactionType.expense:
                continue
            if transaction.category and transaction.category != "Other":
                continue
            text = " ".join(
                part
                for part in (transaction.description, transaction.merchant)
                if part
            )
            category = category_service.category_for(text)
            if category == "Other":
                continue
            transaction.category = category
            changed = True
        if changed:
            await self.session.commit()

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
            if amount != 0
        ]

    def _category_details(
        self,
        receipts: list[object],
        transactions: list[object],
    ) -> dict[str, dict[str, object]]:
        categories = {row["label"] for row in self._expense_by_category(receipts, transactions)}
        details = {}
        for category in categories:
            rows = self._category_expense_rows(category, receipts, transactions)
            detail: dict[str, object] = {
                "category": category,
                "expenses": rows,
            }
            if category == "Food":
                detail["store_breakdown"] = self._food_store_breakdown(receipts, transactions)
                detail["nutrient_breakdown"] = self._food_nutrient_breakdown(receipts)
            details[category] = detail
        return details

    def _category_expense_rows(
        self,
        category: str,
        receipts: list[object],
        transactions: list[object],
    ) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        if category == "Food":
            rows.extend(
                {
                    "date": self.receipt_repository.effective_receipt_date(receipt).isoformat(),
                    "description": f"Receipt: {receipt.shop_name or 'Unknown'}",
                    "merchant": receipt.shop_name or "Unknown",
                    "amount": self._money(
                        self.receipt_repository.amount_as_decimal(receipt.total_amount)
                    ),
                    "source": "receipt",
                }
                for receipt in receipts
            )
        for transaction in transactions:
            if transaction.transaction_type != TransactionType.expense:
                continue
            if (transaction.category or "Other") != category:
                continue
            rows.append(
                {
                    "date": self._dashboard_transaction_date(transaction).isoformat(),
                    "description": transaction.description,
                    "merchant": transaction.merchant or "",
                    "amount": self._money(
                        self.finance_repository.amount_as_decimal(transaction.amount)
                    ),
                    "source": transaction.source,
                }
            )
        return sorted(rows, key=lambda row: row["date"], reverse=True)

    def _food_store_breakdown(
        self,
        receipts: list[object],
        transactions: list[object],
    ) -> list[dict[str, object]]:
        totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for receipt in receipts:
            totals[receipt.shop_name or "Unknown"] += self.receipt_repository.amount_as_decimal(
                receipt.total_amount
            )
        for transaction in transactions:
            if transaction.transaction_type != TransactionType.expense:
                continue
            if transaction.category != "Food":
                continue
            store = transaction.merchant or transaction.description
            totals[store or "Unknown"] += self.finance_repository.amount_as_decimal(
                transaction.amount
            )
        return self._breakdown_rows(totals)

    def _food_nutrient_breakdown(self, receipts: list[object]) -> list[dict[str, object]]:
        totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for receipt in receipts:
            for item in receipt.items:
                amount = self.receipt_repository.amount_as_decimal(item.total_amount)
                if amount == 0:
                    continue
                totals[self._food_group(item.name)] += amount
        return self._breakdown_rows(totals)

    def _breakdown_rows(self, totals: dict[str, Decimal]) -> list[dict[str, object]]:
        total = sum(totals.values(), Decimal("0"))
        if total == 0:
            return []
        rows = []
        for label, amount in sorted(totals.items(), key=lambda item: item[1], reverse=True):
            rows.append(
                {
                    "label": label,
                    "value": self._money(amount),
                    "raw_value": float(amount),
                    "percent": round(float(amount / total * Decimal("100")), 1),
                }
            )
        return rows

    @staticmethod
    def _food_group(name: str) -> str:
        lowered = name.lower()
        groups = {
            "Meat & Fish": (
                "chicken", "frango", "beef", "pork", "carne", "fish", "peixe",
                "salmon", "tuna", "atum", "ham",
            ),
            "Vegetables": (
                "broccoli", "brócolo", "tomato", "tomate", "lettuce", "alface",
                "onion", "cebola", "pepper", "cenoura", "carrot", "vegetable",
            ),
            "Fruit": (
                "apple", "maçã", "banana", "orange", "laranja", "avocado",
                "abacate", "fruit", "berries", "morang",
            ),
            "Bread & Grains": (
                "bread", "pão", "rice", "arroz", "pasta", "massa", "oat",
                "aveia", "flour", "cereal", "lentil",
            ),
            "Dairy": (
                "milk", "leite", "cheese", "queijo", "yogurt", "iogurte",
                "butter", "manteiga", "cream",
            ),
            "Snacks & Sweets": (
                "chocolate", "cookie", "biscuit", "bolacha", "sweet", "candy",
                "snack", "chips", "crisps",
            ),
            "Drinks": (
                "water", "água", "agua", "juice", "sumo", "cola", "tonic",
                "beer", "wine", "vinho",
            ),
        }
        for group, tokens in groups.items():
            if any(token in lowered for token in tokens):
                return group
        return "Other Food"

    def _income_by_category(self, transactions: list[object]) -> list[dict[str, str]]:
        totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for transaction in transactions:
            if transaction.transaction_type != TransactionType.income:
                continue
            totals[transaction.category or "Income"] += self.finance_repository.amount_as_decimal(
                transaction.amount
            )
        return [
            {"label": category, "value": self._money(amount)}
            for category, amount in sorted(totals.items(), key=lambda item: item[1], reverse=True)
            if amount != 0
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

    def _promotions_for_household_items(
        self,
        *,
        receipts: list[object],
        pending_items: list[object],
        quotes: list[object],
    ) -> list[dict[str, str]]:
        household_item_names = {
            self._normalise(receipt_item.name)
            for receipt in receipts
            for receipt_item in receipt.items
            if receipt_item.name
        }
        household_item_names.update(
            self._normalise(item.name)
            for item in pending_items
            if item.name
        )
        promotions: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()

        for quote in quotes:
            if not quote.is_promotion:
                continue
            price = self._decimal_price(quote.price)
            if price is None or price <= 0:
                continue
            if not self._valid_quote_for_item(item_store="", quote=quote):
                continue
            quote_name = self._normalise(quote.item_name)
            product_name = self._normalise(quote.product_name)
            if not self._matches_household_item(
                quote_name=quote_name,
                product_name=product_name,
                household_item_names=household_item_names,
            ):
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

    def _recent_transactions(self, transactions: list[object]) -> list[dict[str, str]]:
        expense_transactions = [
            transaction
            for transaction in transactions
            if transaction.transaction_type == TransactionType.expense
        ]
        return [
            {
                "id": str(transaction.id),
                "type": transaction.transaction_type.value,
                "category": transaction.category or "Other",
                "merchant": transaction.merchant or "",
                "description": transaction.description,
                "date": self._dashboard_transaction_date(transaction).isoformat(),
                "occurred_on": transaction.occurred_on.isoformat(),
                "amount": self._money(self.finance_repository.amount_as_decimal(transaction.amount)),
            }
            for transaction in sorted(
                expense_transactions,
                key=lambda transaction: (self._dashboard_transaction_date(transaction), transaction.created_at),
                reverse=True,
            )[:30]
        ]

    async def _activity(self, *, household_id: UUID) -> list[dict[str, str]]:
        entries = await self.activity_repository.list_recent(household_id=household_id, limit=10)
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
                "category": str(entry.metadata_json.get("category") or ""),
                "date": entry.created_at.isoformat(),
                "activity_class": self._activity_class(entry.entity_type),
            }
            for entry in entries
        ]

    async def _task_stats(self, *, household_id: UUID, start: date, end: date) -> dict[str, object]:
        result = await self.session.execute(
            select(TaskCompletion).where(
                TaskCompletion.household_id == household_id,
                TaskCompletion.completed_on >= start,
                TaskCompletion.completed_on <= end,
            )
        )
        completions = list(result.scalars().all())
        done = sum(1 for completion in completions if completion.status == TaskStatus.done)
        skipped = sum(1 for completion in completions if completion.status == TaskStatus.skipped)
        moved = sum(1 for completion in completions if completion.status == TaskStatus.moved)
        total = done + skipped + moved
        completion_rate = round(done / total * 100) if total else 0
        return {
            "done": done,
            "skipped": skipped,
            "moved": moved,
            "total": total,
            "completion_rate": completion_rate,
        }

    async def activity_page(
        self,
        *,
        household_id: UUID,
        page: int,
        page_size: int,
        search: str | None = None,
        entity_type: str | None = None,
        action: str | None = None,
        category: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict[str, object]:
        entries = await self.activity_repository.list_for_household(household_id=household_id, limit=1000)
        users = await self.user_repository.list_users()
        user_names = {
            user.id: user.username or user.first_name or str(user.telegram_user_id)
            for user in users
        }
        filtered = []
        search_value = self._normalise(search)
        entity_value = self._normalise(entity_type)
        action_value = self._normalise(action)
        category_value = self._normalise(category)

        for entry in entries:
            entry_category = str(entry.metadata_json.get("category") or "")
            created_date = entry.created_at.date()
            if search_value and search_value not in self._normalise(entry.summary):
                continue
            if entity_value and entity_value != self._normalise(entry.entity_type):
                continue
            if action_value and action_value != self._normalise(entry.action.value):
                continue
            if category_value and category_value != self._normalise(entry_category):
                continue
            if start_date and created_date < start_date:
                continue
            if end_date and created_date > end_date:
                continue
            filtered.append(entry)

        page = max(page, 1)
        page_size = min(max(page_size, 5), 100)
        start = (page - 1) * page_size
        items = filtered[start : start + page_size]
        categories = sorted(
            {
                str(entry.metadata_json.get("category") or "")
                for entry in entries
                if entry.metadata_json.get("category")
            }
        )
        entity_types = sorted({entry.entity_type for entry in entries})
        actions = sorted({entry.action.value for entry in entries})
        return {
            "page": page,
            "page_size": page_size,
            "total": len(filtered),
            "pages": (len(filtered) + page_size - 1) // page_size if filtered else 1,
            "categories": categories,
            "entity_types": entity_types,
            "actions": actions,
            "items": [
                {
                    "action": entry.action.value,
                    "entity_type": entry.entity_type,
                    "actor": user_names.get(entry.user_id, "system"),
                    "summary": entry.summary,
                    "category": str(entry.metadata_json.get("category") or ""),
                    "date": entry.created_at.isoformat(),
                    "activity_class": self._activity_class(entry.entity_type),
                }
                for entry in items
            ],
        }

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
        end_month: date,
        today: date,
    ) -> list[dict[str, object]]:
        month_count = max(1, min(24, (end_month.year - start_month.year) * 12 + end_month.month - start_month.month + 1))
        months = [self._shift_month(start_month, offset) for offset in range(month_count)]
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
            transaction_date = self._dashboard_transaction_date(transaction)
            key = (transaction_date.year, transaction_date.month)
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
            transaction_date = self._dashboard_transaction_date(transaction)
            transaction_month = (transaction_date.year, transaction_date.month)
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
    def _dashboard_transaction_date(transaction: object) -> date:
        occurred_on = transaction.occurred_on
        created_date = transaction.created_at.date()
        # Bank screenshots sometimes parse statement dates that are outside the period the
        # user is importing. Keep those rows visible in the import month instead of hiding
        # them from the dashboard total the user just checked.
        if occurred_on.year == created_date.year and occurred_on.month == created_date.month:
            return occurred_on
        return created_date

    @staticmethod
    def _activity_class(entity_type: str) -> str:
        normalized = DashboardService._normalise(entity_type)
        if "shopping" in normalized:
            return "shopping"
        if "task" in normalized:
            return "task"
        if "routine" in normalized:
            return "routine"
        if "receipt" in normalized:
            return "receipt"
        if "transaction" in normalized or "finance" in normalized:
            return "finance"
        return ""

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
            if price is None or price <= 0:
                continue
            candidates.append((price, DashboardService._store_rank(quote.store_name), quote))

        if not candidates:
            return None
        return sorted(candidates, key=lambda candidate: (candidate[0], candidate[1]))[0][2]

    @staticmethod
    def _valid_quote_for_item(*, item_store: str, quote: object) -> bool:
        store = DashboardService._normalise(quote.store_name)
        product_name = DashboardService._normalise(quote.product_name)
        if (
            "pesquisou por" in product_name
            or "search" in product_name
            or "elementsbytagname" in product_name
            or product_name in {"document", "window", "undefined"}
        ):
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
    def _matches_household_item(
        *,
        quote_name: str,
        product_name: str,
        household_item_names: set[str],
    ) -> bool:
        if not quote_name:
            return False
        for item_name in household_item_names:
            if not item_name:
                continue
            for related_name in DashboardService._related_item_names(item_name):
                if quote_name in related_name or related_name in quote_name:
                    return True
                if product_name and related_name in product_name:
                    return True
                if product_name and quote_name in related_name:
                    return True
        return False

    @staticmethod
    def _related_item_names(item_name: str) -> set[str]:
        names = {item_name}
        aliases = {
            "cashew": {"caju"},
            "cashews": {"caju"},
            "walnut": {"noz", "nozes"},
            "walnuts": {"noz", "nozes"},
            "oat": {"aveia"},
            "oats": {"aveia"},
            "tonic water": {"agua tonica", "água tónica"},
            "cottage cheese": {"queijo cottage", "requeijao", "requeijão"},
        }
        for source, replacements in aliases.items():
            if source in item_name:
                names.update(replacements)
        return names

    @staticmethod
    def _names_match(left: str, right: str) -> bool:
        for related_name in DashboardService._related_item_names(left):
            if related_name in right or right in related_name:
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
