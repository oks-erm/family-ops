from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import ActivityAction, TransactionType
from app.db.repositories.activity import ActivityRepository
from app.db.repositories.finance import FinanceRepository
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.users import UserRepository
from app.services.ai_router import AiRouter
from app.services.finance_category_service import FinanceCategoryService


@dataclass(frozen=True)
class ParsedFinanceMessage:
    transaction_type: TransactionType
    description: str
    amount: str
    category: str
    merchant: str | None = None


class FinanceService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.finance_repository = FinanceRepository(session)
        self.household_repository = HouseholdRepository(session)
        self.activity_repository = ActivityRepository(session)
        self.category_service = FinanceCategoryService()
        self.ai_router = AiRouter(settings)

    async def add_manual_transaction(
        self,
        *,
        user_id: UUID,
        parsed: ParsedFinanceMessage,
        occurred_on: date,
    ) -> str:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before finance actions can run.")
        household = await self.household_repository.ensure_household_for_user(user=user)
        transaction = await self.finance_repository.create_transaction(
            user_id=user_id,
            household_id=household.id,
            transaction_type=parsed.transaction_type,
            category=parsed.category,
            merchant=parsed.merchant,
            description=parsed.description,
            amount=parsed.amount,
            currency="EUR",
            occurred_on=occurred_on,
            source="manual",
            raw_data={},
            commit=False,
        )
        await self.activity_repository.log(
            household_id=household.id,
            user_id=user_id,
            action=ActivityAction.created,
            entity_type="financial_transaction",
            entity_id=transaction.id,
            summary=f"Added {parsed.transaction_type.value}: {parsed.description} {parsed.amount} EUR",
            metadata={"category": parsed.category},
            commit=False,
        )
        await self.session.commit()
        return f"Saved {parsed.transaction_type.value}: {parsed.description} ({parsed.category}) {parsed.amount} EUR."

    async def extract_bank_screenshot(
        self,
        *,
        user_id: UUID,
        image_bytes: bytes,
        mime_type: str,
        occurred_on: date,
    ) -> str | None:
        extraction = await self.ai_router.extract_bank_transactions(
            image_bytes=image_bytes,
            mime_type=mime_type,
        )
        if extraction.data is None:
            return None
        transactions = extraction.data.get("transactions")
        if not isinstance(transactions, list) or not transactions:
            return None

        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before finance actions can run.")
        household = await self.household_repository.ensure_household_for_user(user=user)
        saved = []
        for item in transactions:
            if not isinstance(item, dict):
                continue
            parsed = self._parsed_from_extracted(item)
            if parsed is None:
                continue
            item_date = self._parse_date(item.get("occurred_on")) or occurred_on
            transaction = await self.finance_repository.create_transaction(
                user_id=user_id,
                household_id=household.id,
                transaction_type=parsed.transaction_type,
                category=parsed.category,
                merchant=parsed.merchant,
                description=parsed.description,
                amount=parsed.amount,
                currency="EUR",
                occurred_on=item_date,
                source="bank_screenshot",
                raw_data=item,
                commit=False,
            )
            await self.activity_repository.log(
                household_id=household.id,
                user_id=user_id,
                action=ActivityAction.created,
                entity_type="financial_transaction",
                entity_id=transaction.id,
                summary=f"Imported {parsed.transaction_type.value}: {parsed.description} {parsed.amount} EUR",
                metadata={"category": parsed.category, "source": "bank_screenshot"},
                commit=False,
            )
            saved.append(parsed)
        if not saved:
            return None
        await self.session.commit()
        lines = [f"Saved {len(saved)} bank transaction(s):"]
        lines.extend(
            f"- {item.transaction_type.value}: {item.description} ({item.category}) {item.amount} EUR"
            for item in saved[:12]
        )
        if len(saved) > 12:
            lines.append(f"...and {len(saved) - 12} more.")
        return "\n".join(lines)

    def parse_manual_transactions(self, text: str) -> list[ParsedFinanceMessage]:
        parts = [
            part.strip()
            for part in re.split(r"[\n;]+", text)
            if part.strip()
        ]
        if len(parts) <= 1:
            parsed = self.parse_manual_transaction(text)
            return [parsed] if parsed is not None else []
        parsed_items = []
        for part in parts:
            parsed = self.parse_manual_transaction(part)
            if parsed is not None:
                parsed_items.append(parsed)
        return parsed_items

    def parse_manual_transaction(self, text: str) -> ParsedFinanceMessage | None:
        stripped = text.strip()
        match = re.search(r"(.+?)\s+([€¢]?\s*\d+(?:[,.]\d{1,2})?|\d+(?:[,.]\d{1,2})?\s*(?:eur|€|¢))\s*$", stripped, re.I)
        if not match:
            return None
        description = self.clean_transaction_description(match.group(1))
        amount = self.clean_amount(match.group(2))
        if amount is None or not description:
            return None
        lowered = description.lower()
        is_income = any(
            token in lowered
            for token in (
                "income",
                "salary",
                "paid",
                "payment from",
                "received",
                "revenue",
                "paycheck",
                "wage",
                "client paid",
                "salario",
                "salário",
                "ordenado",
                "recebi",
            )
        )
        if any(token in lowered for token in ("expense", "spent", "petrol", "commute", "tax", "iva", "rent")):
            is_income = False
        transaction_type = TransactionType.income if is_income else TransactionType.expense
        category = self.category_service.category_for(description, is_income=is_income)
        return ParsedFinanceMessage(
            transaction_type=transaction_type,
            description=description,
            amount=amount,
            category=category,
            merchant=description,
        )

    def _parsed_from_extracted(self, item: dict[str, Any]) -> ParsedFinanceMessage | None:
        description = self.clean_transaction_description(
            str(item.get("description") or item.get("merchant") or "")
        )
        amount = self.clean_amount(item.get("amount"))
        if not description or amount is None:
            return None
        raw_type = str(item.get("transaction_type") or "").lower()
        transaction_type = TransactionType.income if raw_type == "income" else TransactionType.expense
        category = str(item.get("category") or "").strip()
        suggested_category = self.category_service.category_for(
            description,
            is_income=transaction_type == TransactionType.income,
        )
        if not category or category == "Other":
            category = suggested_category
        elif suggested_category != "Other":
            category = suggested_category
        if category not in self.category_service.categories() and category != "Income":
            category = self.category_service.category_for(
                description,
                is_income=transaction_type == TransactionType.income,
            )
        return ParsedFinanceMessage(
            transaction_type=transaction_type,
            description=description,
            amount=amount,
            category=category,
            merchant=self._optional_string(item.get("merchant")),
        )

    @staticmethod
    def clean_amount(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).lower().replace("eur", "").replace("€", "").replace("¢", "").strip()
        text = text.replace(",", ".")
        try:
            amount = Decimal(text).copy_abs()
        except InvalidOperation:
            return None
        return str(amount.quantize(Decimal("0.01")))

    @staticmethod
    def clean_transaction_description(value: str) -> str:
        text = value.strip(" :.-")
        text = re.sub(
            r"^(?:expense|spent|spend|paid|pay|income|received|recebi|despesa|gasto|gastei|rendimento|entrada)\s+",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s+", " ", text).strip(" :.-")
        return text

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
