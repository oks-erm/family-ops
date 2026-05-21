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
    item_removed = "item_removed"
    item_moved = "item_moved"
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

        planning_query = self._parse_planning_query(text)
        if planning_query is not None:
            return await self._planning_summary(user_id=user_id, period=planning_query)

        move_request = self._parse_move_request(text)
        if move_request is not None:
            return await self._move_item(user_id=user_id, request=move_request)

        remove_request = self._parse_remove_request(text)
        if remove_request is not None:
            return await self._remove_item(user_id=user_id, request=remove_request)

        work_hours = self._parse_work_hours(text)
        if work_hours is not None:
            work_start, work_end, plan_date = work_hours
            return await self._update_work_hours(
                user_id=user_id,
                text=text,
                work_start=work_start,
                work_end=work_end,
                plan_date=plan_date,
            )

        planning_note = self._parse_planning_note(text)
        if planning_note is not None:
            return await self._store_planning_note(
                user_id=user_id,
                text=planning_note[0],
                explicit_date=planning_note[1],
            )

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
                "I can handle shopping, tasks, receipts, and plans. Try: "
                "'Need eggs from Lidl', 'I need to call dentist tomorrow', "
                "'plan tomorrow', or 'plan cook dinner today'."
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

    async def _store_planning_note(
        self,
        *,
        user_id: UUID,
        text: str,
        explicit_date: date | None = None,
    ) -> AssistantResponse:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before assistant actions can run.")
        from app.utils.datetime import now_in_timezone

        household_id = await self._household_id_for_user(user_id=user_id)
        today = now_in_timezone(user.timezone).date()
        plan_date = explicit_date or self._date_from_text(text, user.timezone) or today
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
            text=f"Stored as a planning note for {plan_date.isoformat()}.\n\n" + plan_text,
        )

    async def _update_work_hours(
        self,
        *,
        user_id: UUID,
        text: str,
        work_start: time,
        work_end: time,
        plan_date: date | None,
    ) -> AssistantResponse:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before assistant actions can run.")
        household_id = await self._household_id_for_user(user_id=user_id)
        plan_date = plan_date or self._date_from_text(text, user.timezone) or self._tomorrow_for_user_timezone(user.timezone)
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
            text=f"Updated work hours for {plan_date.isoformat()}.\n\n" + plan_text,
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
        elif period == "tomorrow":
            start = today + timedelta(days=1)
            end = start
        else:  # "today" or legacy "day"
            start = today
            end = today

        tasks = await self.task_repository.list_pending_for_user(user_id=user_id, through_date=end)
        household_id = await self._household_id_for_user(user_id=user_id)

        if period == "week":
            heading = "This week"
        elif period == "tomorrow":
            heading = "Tomorrow"
        else:
            heading = "Today"
        lines = [heading]
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
        calendar_events = await CalendarService(self.session).list_events_for_day(
            household_id=household_id,
            day=plan_date,
            timezone=user.timezone,
        )
        from app.utils.datetime import now_in_timezone

        today = now_in_timezone(user.timezone).date()
        current_time = now_in_timezone(user.timezone).time() if plan_date == today else None

        planning_service = PlanningService()
        plan = planning_service.build_daily_plan(
            PlanningInput(
                user_id=user_id,
                plan_date=plan_date,
                work_start=conversation.work_start if conversation else None,
                work_end=conversation.work_end if conversation else None,
                unusual_notes=conversation.unusual_notes if conversation else None,
                tasks=[task.title for task in tasks],
                calendar_events=calendar_events,
                current_time=current_time,
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

    async def _remove_item(self, *, user_id: UUID, request: dict[str, str]) -> AssistantResponse:
        name = request["name"]
        target = request["target"]
        household_id = await self._household_id_for_user(user_id=user_id)

        if target in {"shopping", "any"}:
            removed_items = await self.shopping_repository.remove_pending_items_by_names(
                household_id=household_id,
                item_names=[name],
            )
            if removed_items:
                item_text = ", ".join(item.name for item in removed_items)
                return AssistantResponse(
                    intent=AssistantIntent.item_removed,
                    text=f"Removed from shopping list: {item_text}.",
                )

        if target in {"task", "plan", "any"}:
            removed_task = await self.task_repository.remove_pending_by_title(
                user_id=user_id,
                title=name,
            )
            if removed_task is not None:
                return AssistantResponse(
                    intent=AssistantIntent.item_removed,
                    text=f"Removed from tasks: {removed_task.title}.",
                )

        return AssistantResponse(
            intent=AssistantIntent.unknown,
            text=f"I could not find '{name}' in the pending shopping list or tasks.",
        )

    async def _move_item(self, *, user_id: UUID, request: dict[str, str]) -> AssistantResponse:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before assistant actions can run.")
        household_id = await self._household_id_for_user(user_id=user_id)
        name = request["name"]
        target = request["target"]
        due_date = self._date_from_text(request.get("date_text", ""), user.timezone)

        if target == "tomorrow":
            moved = await self.task_repository.move_pending_by_title(
                user_id=user_id,
                title=name,
                due_date=due_date or self._tomorrow_for_user_timezone(user.timezone),
            )
            if moved is None:
                return AssistantResponse(
                    intent=AssistantIntent.unknown,
                    text=f"I could not find a pending task called '{name}' to move.",
                )
            return AssistantResponse(
                intent=AssistantIntent.item_moved,
                text=f"Moved task to {moved.due_date.isoformat()}: {moved.title}.",
            )

        if target in {"task", "plan"}:
            removed_items = await self.shopping_repository.remove_pending_items_by_names(
                household_id=household_id,
                item_names=[name],
            )
            if not removed_items:
                return AssistantResponse(
                    intent=AssistantIntent.unknown,
                    text=f"I could not find '{name}' on the shopping list to move.",
                )
            created = await self.task_repository.create_task(
                user_id=user_id,
                household_id=household_id,
                title=removed_items[0].name,
                due_date=due_date,
            )
            return AssistantResponse(
                intent=AssistantIntent.item_moved,
                text=f"Moved from shopping list to tasks: {created.title}.",
            )

        if target == "shopping":
            removed_task = await self.task_repository.remove_pending_by_title(
                user_id=user_id,
                title=name,
            )
            if removed_task is None:
                return AssistantResponse(
                    intent=AssistantIntent.unknown,
                    text=f"I could not find a pending task called '{name}' to move.",
                )
            saved = await self.shopping_repository.add_item(
                user_id=user_id,
                household_id=household_id,
                name=removed_task.title,
                store_name=None,
            )
            return AssistantResponse(
                intent=AssistantIntent.item_moved,
                text=f"Moved from tasks to shopping list: {saved.name}.",
            )

        return AssistantResponse(
            intent=AssistantIntent.unknown,
            text="I can move an item to tomorrow, to tasks, to plan, or to shopping.",
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
        lowered = stripped.lower()
        if AssistantService._looks_like_shopping_request(stripped):
            return None
        if AssistantService._parse_planning_query(stripped) is not None:
            return None
        patterns = (
            r"^(?:task|todo|to do):\s+(.+)$",
            r"^(?:add task|create task|remind me to)\s+(.+)$",
            r"^(?:i need to|we need to|need to|i have to|we have to|have to|i should|we should)\s+(.+)$",
            r"^(?:can you remind me to|please remind me to)\s+(.+)$",
        )
        for pattern in patterns:
            match = re.match(pattern, stripped, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip(" .")
        if any(verb in lowered for verb in AssistantService._ACTION_VERBS):
            if any(ref in lowered for ref in ("today", "tomorrow", "tonight", "this week", "next week")):
                return stripped
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
    def _parse_work_hours(text: str) -> tuple[time, time, date | None] | None:
        lowered = text.lower()
        if "work" not in lowered and "working" not in lowered:
            return None
        matches = list(re.finditer(r"\b([01]?\d|2[0-3])(?::|\.|h)?([0-5]\d)?\b", lowered))
        if len(matches) < 2:
            return None
        start_match, end_match = matches[0], matches[1]
        start = time(hour=int(start_match.group(1)), minute=int(start_match.group(2) or 0))
        end = time(hour=int(end_match.group(1)), minute=int(end_match.group(2) or 0))
        return start, end, None

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
        # "plan today", "plan tomorrow", "plan for tomorrow", "plans for this week", etc.
        time_ref_match = re.match(
            r"^plans?\s+(?:for\s+)?(today|tonight|tomorrow|this week|next week)$",
            lowered,
        )
        if time_ref_match:
            ref = time_ref_match.group(1)
            if "week" in ref:
                return "week"
            if "tomorrow" in ref:
                return "tomorrow"
            return "today"
        if lowered in {"plan", "plans"}:
            return "today"
        query_markers = (
            "what do i have to do",
            "what do we have to do",
            "what should i do",
            "what should we do",
            "what is my plan",
            "what's my plan",
            "what's planned",
            "what is planned",
            "show my plan",
            "show me my plan",
            "plans for",
            "plan for",
        )
        if not any(marker in lowered for marker in query_markers):
            return None
        if "week" in lowered:
            return "week"
        if "tomorrow" in lowered:
            return "tomorrow"
        return "today"

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
    def _parse_planning_note(text: str) -> tuple[str, date | None] | None:
        stripped = text.strip()
        match = re.match(r"^plan\s+(.+)$", stripped, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        content = match.group(1).strip()
        # Time references are queries, not notes
        if re.match(
            r"^(today|tonight|tomorrow|this week|next week|for today|for tomorrow|for this week)$",
            content,
            flags=re.IGNORECASE,
        ):
            return None
        return content, None

    @staticmethod
    def _parse_remove_request(text: str) -> dict[str, str] | None:
        stripped = text.strip()
        patterns = (
            r"^(?:remove|delete|drop)\s+(.+?)\s+from\s+(shopping list|shopping|tasks?|plan)$",
            r"^(?:remove|delete|drop)\s+(.+)$",
        )
        for pattern in patterns:
            match = re.match(pattern, stripped, flags=re.IGNORECASE)
            if not match:
                continue
            target = "any"
            if len(match.groups()) >= 2 and match.group(2):
                raw_target = match.group(2).lower()
                if "shop" in raw_target:
                    target = "shopping"
                elif "task" in raw_target:
                    target = "task"
                elif "plan" in raw_target:
                    target = "plan"
            return {"name": match.group(1).strip(" ."), "target": target}
        return None

    @staticmethod
    def _parse_move_request(text: str) -> dict[str, str] | None:
        stripped = text.strip()
        lowered = stripped.lower()
        match = re.match(
            r"^(?:move|change|reschedule)\s+(.+?)\s+(?:to|for)\s+(tomorrow|today|tonight|this week|next week)$",
            stripped,
            flags=re.IGNORECASE,
        )
        if match:
            return {
                "name": match.group(1).strip(" ."),
                "target": "tomorrow" if "tomorrow" in match.group(2).lower() else "tomorrow",
                "date_text": match.group(2),
            }
        match = re.match(
            r"^(?:move|change)\s+(.+?)\s+from\s+(?:shopping list|shopping)\s+to\s+(tasks?|plan)(?:\s+(today|tomorrow))?$",
            stripped,
            flags=re.IGNORECASE,
        )
        if match:
            return {
                "name": match.group(1).strip(" ."),
                "target": "task" if "task" in match.group(2).lower() else "plan",
                "date_text": match.group(3) or "",
            }
        match = re.match(
            r"^(?:move|change)\s+(.+?)\s+from\s+(?:tasks?|plan)\s+to\s+(?:shopping list|shopping)$",
            stripped,
            flags=re.IGNORECASE,
        )
        if match:
            return {"name": match.group(1).strip(" ."), "target": "shopping", "date_text": ""}
        if lowered.startswith("move ") and " tomorrow" in lowered:
            return {
                "name": re.sub(r"\s+(?:to|for)?\s*tomorrow$", "", stripped[5:], flags=re.IGNORECASE).strip(" ."),
                "target": "tomorrow",
                "date_text": "tomorrow",
            }
        return None

    @staticmethod
    def _date_from_text(text: str, timezone: str) -> date | None:
        from app.utils.datetime import now_in_timezone

        lowered = text.lower()
        today = now_in_timezone(timezone).date()
        if any(ref in lowered for ref in ("today", "tonight", "this evening")):
            return today
        if "tomorrow" in lowered:
            return today + timedelta(days=1)
        return None

    _ACTION_VERBS = (
        "cook",
        "clean",
        "call",
        "email",
        "send",
        "book",
        "schedule",
        "pay",
        "finish",
        "start",
        "prepare",
        "workout",
        "exercise",
        "go to",
        "pick up",
        "drop off",
        "wash",
        "fold",
        "fix",
        "organize",
        "study",
        "write",
    )

    @staticmethod
    def _looks_like_shopping_request(text: str) -> bool:
        lowered = text.lower().strip()
        return lowered.startswith(
            (
                "need ",
                "i need ",
                "we need ",
                "buy ",
                "add ",
                "preciso de ",
                "precisamos de ",
                "comprar ",
            )
        ) and not lowered.startswith(("need to ", "i need to ", "we need to "))
