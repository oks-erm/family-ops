from dataclasses import dataclass
from datetime import date, time, timedelta
from enum import StrEnum
import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import ActivityAction, DailyPlanStatus, PlanningConversationState
from app.db.repositories.activity import ActivityRepository
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.planning import PlanningRepository
from app.db.repositories.tasks import TaskRepository
from app.db.repositories.users import UserRepository
from app.db.repositories.shopping import ShoppingRepository
from app.services.calendar_service import CalendarService
from app.services.ai_router import AiRouter
from app.services.analytics_service import AnalyticsService
from app.services.finance_service import FinanceService
from app.services.planning_service import PlanningInput, PlanningService
from app.services.shopping_category_service import ShoppingCategoryService
from app.services.shopping_service import ParsedShoppingItem, ShoppingService


class AssistantIntent(StrEnum):
    add_shopping_item = "add_shopping_item"
    mark_shopping_purchased = "mark_shopping_purchased"
    going_to_store = "going_to_store"
    shopping_summary = "shopping_summary"
    expense_summary = "expense_summary"
    finance_transaction = "finance_transaction"
    planning_note = "planning_note"
    task_created = "task_created"
    unknown = "unknown"


@dataclass(frozen=True)
class AssistantResponse:
    intent: AssistantIntent
    text: str
    metadata: dict[str, str] | None = None


class AssistantService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.shopping_service = ShoppingService()
        self.shopping_category_service = ShoppingCategoryService()
        self.shopping_repository = ShoppingRepository(session)
        self.household_repository = HouseholdRepository(session)
        self.activity_repository = ActivityRepository(session)
        self.planning_repository = PlanningRepository(session)
        self.task_repository = TaskRepository(session)
        self.ai_router = AiRouter(settings)

    async def handle_text(self, *, user_id: UUID, text: str) -> AssistantResponse:
        active_conversation = await self.planning_repository.get_active_conversation(user_id=user_id)
        if active_conversation is not None:
            return await self._handle_planning_answer(user_id=user_id, text=text)

        task_title = self._parse_task_creation(text)
        if task_title is not None:
            return await self._create_task(user_id=user_id, title=task_title)

        shopping_items = self.shopping_service.parse_add_items(text)
        if shopping_items:
            if len(shopping_items) == 1:
                return await self._add_shopping_item(user_id=user_id, item=shopping_items[0])
            return await self._add_shopping_items(user_id=user_id, items=shopping_items)

        store_name = self.shopping_service.parse_store_visit(text)
        if store_name is not None:
            return await self._list_store_items(user_id=user_id, store_name=store_name)

        if self._is_shopping_summary_query(text):
            return await self._shopping_summary(user_id=user_id)

        purchased_items = self.shopping_service.parse_purchased_items(text)
        if purchased_items:
            return await self._mark_purchased(user_id=user_id, item_names=purchased_items)

        finance_service = FinanceService(self.session, self.ai_router.settings)
        finance_transaction = finance_service.parse_manual_transaction(text)
        if finance_transaction is not None:
            user = await UserRepository(self.session).get_by_id(user_id=user_id)
            if user is None:
                raise RuntimeError("User must exist before finance actions can run.")
            from app.utils.datetime import now_in_timezone

            response_text = await finance_service.add_manual_transaction(
                user_id=user_id,
                parsed=finance_transaction,
                occurred_on=now_in_timezone(user.timezone).date(),
            )
            return AssistantResponse(intent=AssistantIntent.finance_transaction, text=response_text)

        expense_query = self._parse_expense_query(text)
        if expense_query is not None:
            period, store_name = expense_query
            return await self._expense_summary(user_id=user_id, period=period, store_name=store_name)

        planning_query = self._parse_planning_query(text)
        if planning_query is not None:
            return await self._planning_summary(user_id=user_id, period=planning_query)

        work_hours = self._parse_work_hours(text)
        if work_hours is not None:
            work_start, work_end = work_hours
            return await self._update_tomorrow_work_hours(
                user_id=user_id,
                text=text,
                work_start=work_start,
                work_end=work_end,
            )

        planning_note = self._parse_planning_note(text)
        if planning_note is not None:
            return await self._store_planning_note(user_id=user_id, text=planning_note)

        ai_result = await self.ai_router.classify_light_intent(text=text)
        ai_intent = (ai_result.data or {}).get("intent")
        if ai_intent == AssistantIntent.planning_note:
            return AssistantResponse(
                intent=AssistantIntent.unknown,
                text="To add a planning note, start the message with: plan ...",
            )

        return AssistantResponse(
            intent=AssistantIntent.unknown,
            text=(
                "I can handle shopping, tasks, receipts, and planning notes now. Try: "
                "'Need eggs from Lidl', 'Task: call dentist', or 'Tomorrow I work 9-17'."
            ),
        )

    async def _handle_planning_answer(self, *, user_id: UUID, text: str) -> AssistantResponse:
        conversation = await self.planning_repository.get_active_conversation(user_id=user_id)
        if conversation is None:
            return AssistantResponse(intent=AssistantIntent.unknown, text="No active planning flow.")

        if conversation.state == PlanningConversationState.awaiting_work_start:
            parsed_time = self._parse_time_answer(text)
            if parsed_time is None:
                return AssistantResponse(
                    intent=AssistantIntent.planning_note,
                    text="Please send the start time as HH:MM, for example 09:00.",
                )
            await self.planning_repository.save_answer(
                conversation=conversation,
                message_text=text,
                work_start=parsed_time,
                next_state=PlanningConversationState.awaiting_work_end,
            )
            return AssistantResponse(
                intent=AssistantIntent.planning_note,
                text="What time do you finish work tomorrow?",
            )

        if conversation.state == PlanningConversationState.awaiting_work_end:
            parsed_time = self._parse_time_answer(text)
            if parsed_time is None:
                return AssistantResponse(
                    intent=AssistantIntent.planning_note,
                    text="Please send the finish time as HH:MM, for example 17:30.",
                )
            await self.planning_repository.save_answer(
                conversation=conversation,
                message_text=text,
                work_end=parsed_time,
                next_state=PlanningConversationState.awaiting_unusual_notes,
            )
            return AssistantResponse(
                intent=AssistantIntent.planning_note,
                text="Anything unusual tomorrow? Appointments, errands, low energy, church, workout?",
            )

        await self.planning_repository.save_answer(
            conversation=conversation,
            message_text=text,
            unusual_notes=text.strip(),
            next_state=PlanningConversationState.complete,
        )
        plan_text = await self._generate_daily_plan_text(user_id=user_id, plan_date=conversation.plan_date)
        return AssistantResponse(
            intent=AssistantIntent.planning_note,
            text="Saved. Draft plan:\n\n" + plan_text,
        )

    async def _store_planning_note(self, *, user_id: UUID, text: str) -> AssistantResponse:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before assistant actions can run.")
        household_id = await self._household_id_for_user(user_id=user_id)
        plan_date = self._tomorrow_for_user_timezone(user.timezone)
        conversation = await self.planning_repository.get_conversation(
            user_id=user_id,
            plan_date=plan_date,
        )
        if conversation is None:
            conversation = await self.planning_repository.start_conversation(
                user_id=user_id,
                household_id=household_id,
                plan_date=plan_date,
            )
        await self.planning_repository.save_answer(
            conversation=conversation,
            message_text=text,
            unusual_notes=text.strip(),
            next_state=PlanningConversationState.complete,
        )
        plan_text = await self._generate_daily_plan_text(user_id=user_id, plan_date=plan_date)
        return AssistantResponse(
            intent=AssistantIntent.planning_note,
            text="Stored as a planning note for tomorrow.\n\n" + plan_text,
        )

    async def _update_tomorrow_work_hours(
        self,
        *,
        user_id: UUID,
        text: str,
        work_start: time,
        work_end: time,
    ) -> AssistantResponse:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before assistant actions can run.")
        household_id = await self._household_id_for_user(user_id=user_id)
        plan_date = self._tomorrow_for_user_timezone(user.timezone)
        conversation = await self.planning_repository.get_conversation(
            user_id=user_id,
            plan_date=plan_date,
        )
        if conversation is None:
            conversation = await self.planning_repository.start_conversation(
                user_id=user_id,
                household_id=household_id,
                plan_date=plan_date,
            )
        await self.planning_repository.save_answer(
            conversation=conversation,
            message_text=text,
            work_start=work_start,
            work_end=work_end,
            next_state=PlanningConversationState.complete,
        )
        conversation.unusual_notes = None
        await self.session.commit()
        plan_text = await self._generate_daily_plan_text(user_id=user_id, plan_date=plan_date)
        return AssistantResponse(
            intent=AssistantIntent.planning_note,
            text="Updated tomorrow's work hours.\n\n" + plan_text,
        )

    async def _planning_summary(self, *, user_id: UUID, period: str) -> AssistantResponse:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before planning can run.")
        from app.utils.datetime import now_in_timezone

        today = now_in_timezone(user.timezone).date()
        if period == "week":
            start = today
            end = today + timedelta(days=6)
        else:
            start = today
            end = today

        tasks = await self.task_repository.list_pending_for_user(user_id=user_id, through_date=end)
        household_id = await self._household_id_for_user(user_id=user_id)
        shopping_items = await self.shopping_repository.list_all_pending_for_household(
            household_id=household_id,
        )

        lines = [f"This {period}" if period == "week" else "Today"]
        due_tasks = [task for task in tasks if task.due_date is not None]
        flexible_tasks = [task for task in tasks if task.due_date is None]
        if due_tasks or flexible_tasks:
            lines.append("")
            lines.append("Tasks")
            for task in due_tasks:
                lines.append(f"- {task.title} ({task.due_date.isoformat()})")
            for task in flexible_tasks[:5]:
                lines.append(f"- {task.title} (flexible)")
        else:
            lines.append("")
            lines.append("Tasks")
            lines.append("- No pending tasks.")

        calendar_lines = []
        current = start
        calendar_service = CalendarService(self.session)
        while current <= end:
            events = await calendar_service.list_events_for_day(
                household_id=household_id,
                day=current,
                timezone=user.timezone,
            )
            for event in events:
                calendar_lines.append(
                    f"- {current.isoformat()} {event.starts_at.strftime('%H:%M')}: {event.title}"
                )
            current += timedelta(days=1)
        if calendar_lines:
            lines.append("")
            lines.append("Calendar")
            lines.extend(calendar_lines[:12])

        if shopping_items:
            lines.append("")
            lines.append("Shopping")
            for item in shopping_items[:8]:
                lines.append(f"- {item.name} ({item.store_name_raw or 'anywhere'})")

        return AssistantResponse(intent=AssistantIntent.planning_note, text="\n".join(lines))

    async def _create_task(self, *, user_id: UUID, title: str) -> AssistantResponse:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before assistant actions can run.")
        due_date = self._parse_due_date(title, user.timezone)
        cleaned_title = self._clean_task_title(title)
        task = await self.task_repository.create_task(
            user_id=user_id,
            household_id=await self._household_id_for_user(user_id=user_id),
            title=cleaned_title,
            due_date=due_date,
        )
        due_text = task.due_date.isoformat() if task.due_date else "no due date"
        return AssistantResponse(
            intent=AssistantIntent.task_created,
            text=f"Task added: {task.title} ({due_text}).",
            metadata={"task_id": str(task.id)},
        )

    async def _generate_daily_plan_text(self, *, user_id: UUID, plan_date: date) -> str:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before planning can run.")
        household_id = await self._household_id_for_user(user_id=user_id)
        conversation = await self.planning_repository.get_conversation(
            user_id=user_id,
            plan_date=plan_date,
        )
        tasks = await self.task_repository.list_pending_for_user(
            user_id=user_id,
            through_date=plan_date,
        )
        shopping_items = await self.shopping_repository.list_all_pending_for_household(
            household_id=household_id,
        )
        calendar_events = await CalendarService(self.session).list_events_for_day(
            household_id=household_id,
            day=plan_date,
            timezone=user.timezone,
        )
        planning_service = PlanningService()
        plan = planning_service.build_daily_plan(
            PlanningInput(
                user_id=user_id,
                plan_date=plan_date,
                work_start=conversation.work_start if conversation else None,
                work_end=conversation.work_end if conversation else None,
                unusual_notes=conversation.unusual_notes if conversation else None,
                tasks=[task.title for task in tasks],
                shopping_items=[
                    f"{item.name} ({item.store_name_raw or 'anywhere'})" for item in shopping_items
                ],
                calendar_events=calendar_events,
            )
        )
        await self.planning_repository.upsert_daily_plan(
            user_id=user_id,
            household_id=household_id,
            plan_date=plan_date,
            work_start=conversation.work_start if conversation else None,
            work_end=conversation.work_end if conversation else None,
            unusual_notes=conversation.unusual_notes if conversation else None,
            plan=plan,
            status=DailyPlanStatus.draft,
        )
        return planning_service.render_plan_message(plan)

    async def _add_shopping_item(
        self,
        *,
        user_id: UUID,
        item: ParsedShoppingItem,
    ) -> AssistantResponse:
        household_id = await self._household_id_for_user(user_id=user_id)
        saved = await self.shopping_repository.add_item(
            user_id=user_id,
            household_id=household_id,
            name=item.name,
            store_name=item.store_name,
        )
        await self.activity_repository.log(
            household_id=household_id,
            user_id=user_id,
            action=ActivityAction.created,
            entity_type="shopping_item",
            entity_id=saved.id,
            summary=f"Added shopping item: {saved.name} ({saved.store_name_raw or 'anywhere'})",
        )
        store_text = saved.store_name_raw or "anywhere"
        return AssistantResponse(
            intent=AssistantIntent.add_shopping_item,
            text=f"Added to shopping list: {saved.name} ({store_text}).",
        )

    async def _add_shopping_items(
        self,
        *,
        user_id: UUID,
        items: list[ParsedShoppingItem],
    ) -> AssistantResponse:
        household_id = await self._household_id_for_user(user_id=user_id)
        saved_items = []
        for item in items:
            saved = await self.shopping_repository.add_item(
                user_id=user_id,
                household_id=household_id,
                name=item.name,
                store_name=item.store_name,
            )
            saved_items.append(saved)
            await self.activity_repository.log(
                household_id=household_id,
                user_id=user_id,
                action=ActivityAction.created,
                entity_type="shopping_item",
                entity_id=saved.id,
                summary=f"Added shopping item: {saved.name} ({saved.store_name_raw or 'anywhere'})",
            )

        item_text = ", ".join(
            f"{item.name} ({item.store_name_raw or 'anywhere'})" for item in saved_items
        )
        return AssistantResponse(
            intent=AssistantIntent.add_shopping_item,
            text=f"Added to shopping list: {item_text}.",
        )

    async def _list_store_items(self, *, user_id: UUID, store_name: str) -> AssistantResponse:
        items = await self.shopping_repository.list_pending_for_store(
            household_id=await self._household_id_for_user(user_id=user_id),
            store_name=store_name,
        )
        if not items:
            return AssistantResponse(
                intent=AssistantIntent.going_to_store,
                text=f"No pending items for {store_name}.",
            )

        lines = [f"Shopping for {store_name}:"]
        lines.extend(f"- {item.name}" for item in items)
        return AssistantResponse(
            intent=AssistantIntent.going_to_store,
            text="\n".join(lines),
        )

    async def _shopping_summary(self, *, user_id: UUID) -> AssistantResponse:
        household_id = await self._household_id_for_user(user_id=user_id)
        items = await self.shopping_repository.list_all_pending_for_household(
            household_id=household_id,
        )
        if not items:
            return AssistantResponse(
                intent=AssistantIntent.shopping_summary,
                text="Nothing is pending on the shopping list.",
            )

        grouped: dict[str, list[str]] = {}
        for item in items:
            category = self.shopping_category_service.category_for(item.name)
            grouped.setdefault(category, []).append(
                f"- {item.name} ({item.store_name_raw or 'anywhere'})"
            )

        preferred_order = ("Food", "Cosmetics", "Clothes", "House")
        lines = ["Shopping list:"]
        for category in preferred_order:
            category_items = grouped.pop(category, [])
            if not category_items:
                continue
            lines.append("")
            lines.append(category)
            lines.extend(category_items)
        for category, category_items in sorted(grouped.items()):
            lines.append("")
            lines.append(category)
            lines.extend(category_items)

        return AssistantResponse(
            intent=AssistantIntent.shopping_summary,
            text="\n".join(lines),
        )

    async def _mark_purchased(self, *, user_id: UUID, item_names: list[str]) -> AssistantResponse:
        household_id = await self._household_id_for_user(user_id=user_id)
        matched_items = await self.shopping_repository.mark_pending_items_purchased_by_names(
            household_id=household_id,
            item_names=item_names,
        )
        if not matched_items:
            return AssistantResponse(
                intent=AssistantIntent.mark_shopping_purchased,
                text="I could not find those items on the pending shopping list.",
            )

        item_text = ", ".join(item.name for item in matched_items)
        for item in matched_items:
            await self.activity_repository.log(
                household_id=household_id,
                user_id=user_id,
                action=ActivityAction.updated,
                entity_type="shopping_item",
                entity_id=item.id,
                summary=f"Marked shopping item as bought: {item.name}",
            )
        return AssistantResponse(
            intent=AssistantIntent.mark_shopping_purchased,
            text=f"Marked as bought: {item_text}.",
        )

    async def _expense_summary(
        self,
        *,
        user_id: UUID,
        period: str,
        store_name: str | None,
    ) -> AssistantResponse:
        from app.utils.datetime import now_in_timezone

        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        today = now_in_timezone(user.timezone if user else "Europe/Lisbon").date()
        text = await AnalyticsService(self.session).grocery_spend_summary(
            household_id=await self._household_id_for_user(user_id=user_id),
            today=today,
            period=period,
            store_name=store_name,
        )
        return AssistantResponse(intent=AssistantIntent.expense_summary, text=text)

    async def _household_id_for_user(self, *, user_id: UUID) -> UUID:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before assistant actions can run.")
        household = await self.household_repository.ensure_household_for_user(user=user)
        return household.id

    @staticmethod
    def _parse_task_creation(text: str) -> str | None:
        stripped = text.strip()
        patterns = (
            r"^(?:task|todo|to do):\s+(.+)$",
            r"^(?:add task|create task|remind me to)\s+(.+)$",
            r"^(?:i need to|we need to)\s+(.+)$",
        )
        for pattern in patterns:
            match = re.match(pattern, stripped, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip(" .")
        return None

    @staticmethod
    def _clean_task_title(title: str) -> str:
        return re.sub(r"\b(today|tomorrow|this week)\b", "", title, flags=re.IGNORECASE).strip(" .")

    @staticmethod
    def _parse_due_date(title: str, timezone: str) -> date | None:
        from app.utils.datetime import now_in_timezone

        today = now_in_timezone(timezone).date()
        lowered = title.lower()
        if "tomorrow" in lowered:
            return today + timedelta(days=1)
        if "today" in lowered:
            return today
        return None

    @staticmethod
    def _parse_time_answer(text: str) -> time | None:
        lowered = text.lower().strip()
        if lowered in {"no work", "off", "day off", "none"}:
            return time(hour=0, minute=0)
        match = re.search(r"\b([01]?\d|2[0-3])(?::|\.|h)?([0-5]\d)?\b", lowered)
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        return time(hour=hour, minute=minute)

    @staticmethod
    def _parse_work_hours(text: str) -> tuple[time, time] | None:
        lowered = text.lower()
        if "work" not in lowered and "working" not in lowered:
            return None
        matches = list(re.finditer(r"\b([01]?\d|2[0-3])(?::|\.|h)?([0-5]\d)?\b", lowered))
        if len(matches) < 2:
            return None
        start_match, end_match = matches[0], matches[1]
        start = time(hour=int(start_match.group(1)), minute=int(start_match.group(2) or 0))
        end = time(hour=int(end_match.group(1)), minute=int(end_match.group(2) or 0))
        return start, end

    @staticmethod
    def _tomorrow_for_user_timezone(timezone: str) -> date:
        from app.utils.datetime import now_in_timezone

        return now_in_timezone(timezone).date() + timedelta(days=1)

    @staticmethod
    def _parse_expense_query(text: str) -> tuple[str, str | None] | None:
        lowered = text.lower()
        if not any(word in lowered for word in ("spend", "spent", "expense", "expenses", "groceries")):
            return None
        period = "this month" if "month" in lowered else "this week"
        store_name = None
        store_match = re.search(r"\b(?:in|at|from)\s+([a-zA-ZÀ-ÿ0-9' -]+)", text)
        if store_match:
            store_name = store_match.group(1).strip(" ?.")
            for suffix in (" this week", " this month", " week", " month"):
                if store_name.lower().endswith(suffix):
                    store_name = store_name[: -len(suffix)].strip()
        return period, store_name

    @staticmethod
    def _parse_planning_query(text: str) -> str | None:
        lowered = text.lower().strip(" ?!.")
        if lowered in {"plan", "plans"}:
            return "day"
        query_markers = (
            "what do i have to do",
            "what do we have to do",
            "what should i do",
            "what should we do",
            "what is my plan",
            "what's my plan",
            "show my plan",
            "show me my plan",
            "plans for",
            "plan for",
        )
        if not any(marker in lowered for marker in query_markers):
            return None
        if "week" in lowered:
            return "week"
        return "day"

    @staticmethod
    def _is_shopping_summary_query(text: str) -> bool:
        lowered = text.lower().strip(" ?!.")
        return (
            "what do i need to buy" in lowered
            or "what do we need to buy" in lowered
            or "what should i buy" in lowered
            or "what should we buy" in lowered
            or "shopping list" in lowered
            or "lista de compras" in lowered
            or "o que preciso comprar" in lowered
            or "o que precisamos comprar" in lowered
            or "o que falta comprar" in lowered
        )

    @staticmethod
    def _parse_planning_note(text: str) -> str | None:
        stripped = text.strip()
        match = re.match(r"^plan\s+(.+)$", stripped, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        return match.group(1).strip()
