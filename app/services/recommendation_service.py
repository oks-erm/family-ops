from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.receipts import ReceiptRepository
from app.db.repositories.recommendations import RecommendationRepository
from app.services.grocery_normalizer import GroceryNormalizer


class RecommendationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.receipt_repository = ReceiptRepository(session)
        self.recommendation_repository = RecommendationRepository(session)
        self.normalizer = GroceryNormalizer()

    async def latest_for_dashboard(self, *, household_id: UUID) -> list[str]:
        recommendation = await self.recommendation_repository.latest_for_household(household_id=household_id)
        if recommendation is None:
            return ["No weekly recommendation has been generated yet. It will update on Monday."]
        return self._actionable_recommendations(list(recommendation.recommendations or []))

    async def generate_weekly_for_household(self, *, household_id: UUID, today: date) -> list[str]:
        period_start, period_end = self._previous_week(today)
        month_start = period_end.replace(day=1)
        previous_week_start = period_start - timedelta(days=7)
        previous_week_end = period_start - timedelta(days=1)

        week_receipts = await self.receipt_repository.list_receipts_between(
            household_id=household_id,
            start_date=period_start,
            end_date=period_end,
        )
        previous_week_receipts = await self.receipt_repository.list_receipts_between(
            household_id=household_id,
            start_date=previous_week_start,
            end_date=previous_week_end,
        )
        month_receipts = await self.receipt_repository.list_receipts_between(
            household_id=household_id,
            start_date=month_start,
            end_date=period_end,
        )

        week_total = self._sum_receipts(week_receipts)
        previous_week_total = self._sum_receipts(previous_week_receipts)
        month_total = self._sum_receipts(month_receipts)
        signals = self._food_signals(week_receipts)
        by_store = self._by_store(week_receipts)
        recommendations = self._build_recommendations(
            week_total=week_total,
            previous_week_total=previous_week_total,
            month_total=month_total,
            by_store=by_store,
            signals=signals,
            receipt_count=len(week_receipts),
        )
        metrics = {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "week_total": self._money(week_total),
            "previous_week_total": self._money(previous_week_total),
            "month_total": self._money(month_total),
            "receipt_count": len(week_receipts),
            "signals": signals,
            "by_store": [{"store": store, "amount": self._money(amount)} for store, amount in by_store],
        }
        await self.recommendation_repository.upsert_weekly_recommendation(
            household_id=household_id,
            period_start=period_start,
            period_end=period_end,
            recommendations=recommendations,
            metrics=metrics,
        )
        return recommendations

    def _build_recommendations(
        self,
        *,
        week_total: Decimal,
        previous_week_total: Decimal,
        month_total: Decimal,
        by_store: list[tuple[str, Decimal]],
        signals: dict[str, int],
        receipt_count: int,
    ) -> list[str]:
        if receipt_count == 0:
            return ["No receipts were confirmed last week, so there is not enough data for a useful recommendation."]

        recommendations: list[str] = []
        if previous_week_total > 0 and week_total > previous_week_total * Decimal("1.20"):
            increase = ((week_total - previous_week_total) / previous_week_total * Decimal("100")).quantize(
                Decimal("1")
            )
            recommendations.append(
                f"Last week grocery spend was {self._money(week_total)}, up {increase}% from the week before; check which trip caused the jump before restocking."
            )
        elif previous_week_total > 0 and week_total < previous_week_total * Decimal("0.85"):
            decrease = ((previous_week_total - week_total) / previous_week_total * Decimal("100")).quantize(
                Decimal("1")
            )
            recommendations.append(
                f"Last week spend was {self._money(week_total)}, down {decrease}% from the week before; repeat the same shopping pattern if meals still felt covered."
            )

        if by_store and week_total > 0:
            top_store, top_amount = by_store[0]
            share = top_amount / week_total
            if share >= Decimal("0.75"):
                percent = (share * Decimal("100")).quantize(Decimal("1"))
                recommendations.append(
                    f"{top_store} made up {percent}% of last week's grocery spend; compare staple prices there before buying everything in one trip."
                )

        if signals["vegetable_items"] < 3:
            recommendations.append(
                f"Only {signals['vegetable_items']} vegetable items appeared in last week's receipts; add vegetables deliberately to the next shop."
            )
        if signals["protein_items"] < 2:
            recommendations.append(
                f"Only {signals['protein_items']} protein items appeared last week; add an easy protein option before planning dinners."
            )
        if signals["snack_items"] >= 3:
            recommendations.append(
                f"{signals['snack_items']} snack-type items appeared last week; choose the one that actually gets eaten and skip duplicate snacks."
            )
        if signals["sweet_items"] >= 2:
            recommendations.append(
                f"{signals['sweet_items']} sweet items appeared last week; avoid restocking sweets until the current ones are gone."
            )
        if signals["eating_out_receipts"] >= 2:
            recommendations.append(
                f"{signals['eating_out_receipts']} eating-out/takeaway receipts appeared last week; plan one fast home meal for the busiest day."
            )

        if not recommendations:
            recommendations.append(
                f"Last week spend was {self._money(week_total)} across {receipt_count} receipt(s), with no strong imbalance detected."
            )
        return self._actionable_recommendations(recommendations)[:5]

    @staticmethod
    def _actionable_recommendations(recommendations: list[str]) -> list[str]:
        blocked_prefixes = (
            "month-to-date",
            "last week spend was",
            "no receipts were confirmed",
        )
        filtered = [
            recommendation
            for recommendation in recommendations
            if not recommendation.strip().casefold().startswith(blocked_prefixes)
        ]
        if filtered:
            return filtered
        return ["No strong pattern yet. Confirm a few more receipts before changing shopping habits."]

    def _food_signals(self, receipts: list[object]) -> dict[str, int]:
        signals = {
            "vegetable_items": 0,
            "protein_items": 0,
            "snack_items": 0,
            "sweet_items": 0,
            "eating_out_receipts": 0,
        }
        for receipt in receipts:
            shop_name = self.normalizer.normalize(receipt.shop_name or "")
            if self._matches_any(shop_name, self._eating_out_terms()):
                signals["eating_out_receipts"] += 1
            for item in receipt.items:
                item_name = self.normalizer.normalize(item.name)
                if self._matches_any(item_name, self._vegetable_terms()):
                    signals["vegetable_items"] += 1
                if self._matches_any(item_name, self._protein_terms()):
                    signals["protein_items"] += 1
                if self._matches_any(item_name, self._snack_terms()):
                    signals["snack_items"] += 1
                if self._matches_any(item_name, self._sweet_terms()):
                    signals["sweet_items"] += 1
        return signals

    def _by_store(self, receipts: list[object]) -> list[tuple[str, Decimal]]:
        totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for receipt in receipts:
            totals[receipt.shop_name or "Unknown"] += self.receipt_repository.amount_as_decimal(
                receipt.total_amount
            )
        return sorted(totals.items(), key=lambda item: item[1], reverse=True)

    def _sum_receipts(self, receipts: list[object]) -> Decimal:
        return sum(
            (self.receipt_repository.amount_as_decimal(receipt.total_amount) for receipt in receipts),
            Decimal("0"),
        )

    @staticmethod
    def _previous_week(today: date) -> tuple[date, date]:
        current_week_start = today - timedelta(days=today.weekday())
        period_start = current_week_start - timedelta(days=7)
        period_end = current_week_start - timedelta(days=1)
        return period_start, period_end

    @staticmethod
    def _matches_any(text: str, terms: set[str]) -> bool:
        tokens = set(text.split())
        return bool(tokens & terms) or any(term in text for term in terms if " " in term)

    @staticmethod
    def _money(value: Decimal) -> str:
        return f"{value.quantize(Decimal('0.01'))} EUR"

    @staticmethod
    def _vegetable_terms() -> set[str]:
        return {
            "avocado",
            "broccoli",
            "tomato",
            "lettuce",
            "spinach",
            "pepper",
            "pimento",
            "courgette",
            "zucchini",
            "carrot",
            "cabbage",
            "couve",
            "onion",
            "garlic",
            "mushroom",
            "vegetable",
            "legume",
            "salad",
        }

    @staticmethod
    def _protein_terms() -> set[str]:
        return {
            "egg",
            "chicken",
            "frango",
            "beef",
            "vaca",
            "porco",
            "pork",
            "fish",
            "peixe",
            "salmon",
            "atum",
            "tuna",
            "tofu",
            "bean",
            "feijao",
            "chickpea",
            "grao",
            "lentil",
            "yogurt",
            "quark",
            "cheese",
            "protein",
        }

    @staticmethod
    def _snack_terms() -> set[str]:
        return {
            "batata",
            "chip",
            "crisp",
            "pringles",
            "lays",
            "lay",
            "snack",
            "aperitivo",
            "bolacha",
            "cracker",
            "popcorn",
            "nacho",
            "salgado",
            "tosta",
            "ondulada",
        }

    @staticmethod
    def _sweet_terms() -> set[str]:
        return {
            "chocolate",
            "candy",
            "sweet",
            "doce",
            "gummy",
            "gelado",
            "ice cream",
            "cake",
            "bolo",
            "biscuit",
            "cookie",
            "nutella",
            "dessert",
            "sobremesa",
        }

    @staticmethod
    def _eating_out_terms() -> set[str]:
        return {
            "restaurant",
            "restaurante",
            "cafe",
            "takeaway",
            "takeout",
            "delivery",
            "uber eats",
            "glovo",
            "bolt food",
            "mcdonalds",
            "burger king",
            "kfc",
            "pizza hut",
            "dominos",
        }
