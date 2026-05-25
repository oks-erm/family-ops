from dataclasses import dataclass
from datetime import date, time, timedelta
from enum import StrEnum
import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import ActivityAction, DailyPlanStatus, PlanningConversationState
from app.db.repositories.activity import ActivityRepository
from app.db.repositories.finance import FinanceRepository
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.planning import PlanningRepository
from app.db.repositories.receipts import ReceiptRepository
from app.db.repositories.routines import RoutineRepository
from app.db.repositories.tasks import TaskRepository
from app.db.repositories.users import UserRepository
from app.db.repositories.shopping import ShoppingRepository
from app.services.calendar_service import CalendarService
from app.services.ai_router import AiRouter
from app.services.analytics_service import AnalyticsService
from app.services.finance_service import FinanceService
from app.services.planning_service import PlannedTaskInput, PlanningInput, PlanningService
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
    metadata: dict[str, object] | None = None


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
        self.routine_repository = RoutineRepository(session)
        self.ai_router = AiRouter(settings)

    async def handle_text(self, *, user_id: UUID, text: str) -> AssistantResponse:
        ai_result = await self.ai_router.classify_light_intent(text=text)
        ai_response = await self._handle_ai_classification(
            user_id=user_id,
            text=text,
            data=ai_result.data,
        )
        if ai_response is not None:
            return ai_response

        conversational_response = self._parse_conversational(text)
        if conversational_response is not None:
            return conversational_response

        sleep_window = self._parse_sleep_window_update(text)
        if sleep_window is not None:
            return await self._update_sleep_window(
                user_id=user_id,
                kind=sleep_window[0],
                value=sleep_window[1],
                plan_date=sleep_window[2],
            )

        completion_request = self._parse_task_completion(text)
        if completion_request is not None:
            return await self._mark_task_done(
                user_id=user_id,
                title=completion_request[0],
                completed_on=completion_request[1],
            )

        task_action_request = self._parse_task_action(text)
        if task_action_request is not None:
            return await self._apply_task_action_by_title(
                user_id=user_id,
                title=task_action_request[0],
                action=task_action_request[1],
                action_date=task_action_request[2],
            )

        fixed_event = self._parse_fixed_event_note(text)
        if fixed_event is not None and (
            fixed_event[1] is not None or text.strip().lower().startswith("change of plans")
        ):
            return await self._store_planning_note(
                user_id=user_id,
                text=fixed_event[0],
                explicit_date=fixed_event[1],
            )

        remove_event_request = self._parse_remove_event_request(text)
        if remove_event_request is not None:
            return await self._remove_planning_event(
                user_id=user_id,
                title=remove_event_request[0],
                plan_date=remove_event_request[1],
            )

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

        active_conversation = await self._current_active_planning_conversation(user_id=user_id)
        if active_conversation is not None:
            return await self._handle_planning_answer(user_id=user_id, text=text)

        planning_query = self._parse_planning_query(text)
        if planning_query is not None:
            return await self._planning_summary(user_id=user_id, period=planning_query)

        if fixed_event is not None:
            return await self._store_planning_note(
                user_id=user_id,
                text=fixed_event[0],
                explicit_date=fixed_event[1],
            )

        routine_update = self._parse_routine_duration_update(text)
        if routine_update is not None:
            return await self._update_routine_duration(user_id=user_id, title=routine_update[0], minutes=routine_update[1])

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

        finance_service = FinanceService(self.session, self.ai_router.settings)
        finance_transactions = finance_service.parse_manual_transactions(text)
        if finance_transactions:
            user = await UserRepository(self.session).get_by_id(user_id=user_id)
            if user is None:
                raise RuntimeError("User must exist before finance actions can run.")
            from app.utils.datetime import now_in_timezone

            response_lines = []
            for finance_transaction in finance_transactions:
                response_lines.append(
                    await finance_service.add_manual_transaction(
                        user_id=user_id,
                        parsed=finance_transaction,
                        occurred_on=now_in_timezone(user.timezone).date(),
                    )
                )
            response_text = "\n".join(response_lines)
            return AssistantResponse(intent=AssistantIntent.finance_transaction, text=response_text)

        expense_query = self._parse_expense_query(text)
        if expense_query is not None:
            period, store_name, query_kind, category = expense_query
            return await self._expense_summary(
                user_id=user_id,
                period=period,
                store_name=store_name,
                query_kind=query_kind,
                category=category,
                original_question=text,
            )

        return AssistantResponse(
            intent=AssistantIntent.unknown,
            text=(
                "I am not sure whether this is shopping, a task, or a plan note. "
                "Please confirm with one of: 'buy ...', 'task ...', or 'plan ...'."
            ),
        )

    async def _handle_planning_answer(self, *, user_id: UUID, text: str) -> AssistantResponse:
        conversation = await self._current_active_planning_conversation(user_id=user_id)
        if conversation is None:
            return AssistantResponse(intent=AssistantIntent.unknown, text="No active planning flow.")

        planning_query = self._parse_planning_query(text)
        if planning_query is not None:
            return await self._planning_summary(user_id=user_id, period=planning_query)

        if conversation.state == PlanningConversationState.awaiting_work_start:
            parsed_time = self._parse_time_answer(text)
            if parsed_time is None:
                semantic_response = await self._semantic_response_during_planning_prompt(
                    user_id=user_id,
                    text=text,
                )
                if semantic_response is not None:
                    return semantic_response
                return AssistantResponse(
                    intent=AssistantIntent.planning_note,
                    text=(
                        "I am still missing tomorrow's work start time. "
                        "Send it when ready, for example 09:00."
                    ),
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
                semantic_response = await self._semantic_response_during_planning_prompt(
                    user_id=user_id,
                    text=text,
                )
                if semantic_response is not None:
                    return semantic_response
                return AssistantResponse(
                    intent=AssistantIntent.planning_note,
                    text=(
                        "I am still missing tomorrow's work finish time. "
                        "Send it when ready, for example 17:30."
                    ),
                )
            conversation = await self.planning_repository.save_answer(
                conversation=conversation,
                message_text=text,
                work_end=parsed_time,
                next_state=PlanningConversationState.awaiting_unusual_notes,
            )
            plan_text = await self._generate_daily_plan_text(user_id=user_id, plan_date=conversation.plan_date)
            return AssistantResponse(
                intent=AssistantIntent.planning_note,
                text=(
                    "Saved work block. Draft plan:\n\n"
                    + plan_text
                    + "\n\nAnything unusual tomorrow? Appointments, errands, low energy, church, workout?"
                ),
            )

        existing_notes = conversation.unusual_notes or ""
        note = text.strip()
        no_notes = note.casefold() in {"no", "nothing", "none", "nope", "nothing unusual"}
        if not no_notes:
            semantic_response = await self._semantic_response_during_planning_prompt(
                user_id=user_id,
                text=text,
                allow_planning_note=False,
            )
            if semantic_response is not None:
                return semantic_response
        combined_notes = existing_notes if no_notes else note
        if existing_notes and not no_notes and note.casefold() not in existing_notes.casefold():
            combined_notes = f"{existing_notes}; {note}"
        await self.planning_repository.save_answer(
            conversation=conversation,
            message_text=text,
            unusual_notes=combined_notes,
            next_state=PlanningConversationState.complete,
        )
        plan_text = await self._generate_daily_plan_text(user_id=user_id, plan_date=conversation.plan_date)
        return AssistantResponse(
            intent=AssistantIntent.planning_note,
            text="Saved. Draft plan:\n\n" + plan_text,
        )

    async def _current_active_planning_conversation(self, *, user_id: UUID):
        conversation = await self.planning_repository.get_active_conversation(user_id=user_id)
        if conversation is None:
            return None
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            return conversation
        from app.utils.datetime import now_in_timezone

        today = now_in_timezone(user.timezone).date()
        if conversation.plan_date <= today:
            conversation.state = PlanningConversationState.complete
            await self.session.commit()
            return None
        return conversation

    async def _semantic_response_during_planning_prompt(
        self,
        *,
        user_id: UUID,
        text: str,
        allow_planning_note: bool = True,
    ) -> AssistantResponse | None:
        ai_result = await self.ai_router.classify_light_intent(text=text)
        intent = ""
        if ai_result.data:
            intent = str(ai_result.data.get("intent") or "").strip()
        if not allow_planning_note and intent == "planning_note":
            return None
        if intent in {
            "add_shopping_item",
            "going_to_store",
            "shopping_summary",
            "mark_shopping_purchased",
            "task_created",
            "planning_query",
            "fixed_event",
            "work_hours_update",
            "sleep_window_update",
            "mark_task_done",
            "remove_item",
            "move_item",
            "expense_query",
            "smalltalk",
            "capability_question",
            "clarify",
        }:
            return await self._handle_ai_classification(
                user_id=user_id,
                text=text,
                data=ai_result.data,
            )
        return None

    async def _handle_ai_classification(
        self,
        *,
        user_id: UUID,
        text: str,
        data: dict[str, object] | None,
    ) -> AssistantResponse | None:
        if not data:
            return None
        confidence = data.get("confidence")
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = 0.0
        if confidence_value < 0.7:
            return None

        intent = str(data.get("intent") or "").strip()
        item = str(data.get("item") or data.get("title") or "").strip()
        date_ref = str(data.get("date_ref") or "").strip().lower()
        store_name = str(data.get("store_name") or "").strip() or None
        target = str(data.get("target") or "any").strip().lower()
        source = str(data.get("source") or "").strip().lower()
        reply = str(data.get("reply") or "").strip()
        start_time = self._parse_ai_time(data.get("start_time"))
        end_time = self._parse_ai_time(data.get("end_time"))
        plan_date = await self._date_from_ai_ref(user_id=user_id, date_ref=date_ref)

        if intent in {"smalltalk", "capability_question", "clarify"} and reply:
            return AssistantResponse(intent=AssistantIntent.unknown, text=reply)
        if intent == "planning_query":
            period = "week" if date_ref == "week" else "tomorrow" if date_ref == "tomorrow" else "today"
            return await self._planning_summary(user_id=user_id, period=period)
        if intent == "work_hours_update" and start_time is not None and end_time is not None:
            return await self._update_work_hours(
                user_id=user_id,
                text=text,
                work_start=start_time,
                work_end=end_time,
                plan_date=plan_date,
            )
        if intent == "sleep_window_update" and start_time is not None:
            kind = str(data.get("kind") or "").strip().lower()
            return await self._update_sleep_window(
                user_id=user_id,
                kind="wake" if kind == "wake" else "sleep",
                value=start_time,
                plan_date=plan_date,
            )
        if intent == "fixed_event" and item and start_time is not None and end_time is not None:
            note = f"{item} from {start_time.strftime('%H:%M')} to {end_time.strftime('%H:%M')}"
            return await self._store_planning_note(user_id=user_id, text=note, explicit_date=plan_date)
        if intent == "task_created" and item:
            normalized_date_ref = "today" if date_ref == "tonight" else date_ref
            title = f"{item} {normalized_date_ref}".strip() if normalized_date_ref in {"today", "tomorrow"} else item
            return await self._create_task(user_id=user_id, title=title)
        if intent == "mark_task_done" and item:
            return await self._mark_task_done(user_id=user_id, title=item, completed_on=plan_date)
        if intent == "planning_note":
            return await self._store_planning_note(user_id=user_id, text=text, explicit_date=plan_date)
        if intent == "add_shopping_item" and item:
            return await self._add_shopping_item(
                user_id=user_id,
                item=ParsedShoppingItem(name=item, store_name=store_name),
            )
        if intent == "mark_shopping_purchased" and item:
            return await self._mark_purchased(user_id=user_id, item_names=[item])
        if intent == "going_to_store" and (store_name or item):
            return await self._list_store_items(user_id=user_id, store_name=store_name or item)
        if intent == "remove_item" and item:
            return await self._remove_item(user_id=user_id, request={"name": item, "target": target or "any"})
        if intent == "move_item" and item:
            if target in {"shopping", "task", "plan"}:
                move_target = target
            elif source in {"shopping", "task", "plan"}:
                move_target = "task" if source == "shopping" else "shopping"
            elif date_ref in {"tomorrow", "today", "tonight"}:
                move_target = date_ref
            elif target and target not in {"any", ""}:
                move_target = target  # store name
            elif store_name:
                move_target = store_name  # store name from store_name field
            else:
                move_target = "task"
            return await self._move_item(
                user_id=user_id,
                request={"name": item, "target": move_target},
            )
        if intent == "shopping_summary":
            return await self._shopping_summary(user_id=user_id)
        if intent == "expense_query":
            ai_kind = str(data.get("kind") or "spend").strip().lower()
            query_kind = "income" if ai_kind == "income" else "spend"
            ai_category = str(data.get("category") or "").strip() or None
            period = self._period_from_date_ref(date_ref)
            return await self._expense_summary(
                user_id=user_id,
                period=period,
                store_name=store_name,
                query_kind=query_kind,
                category=ai_category,
                original_question=text,
            )
        if intent in {"unknown", "clarify"} and reply:
            return AssistantResponse(intent=AssistantIntent.unknown, text=reply)
        return None

    async def _date_from_ai_ref(self, *, user_id: UUID, date_ref: str) -> date | None:
        if not date_ref:
            return None
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            return None
        from app.utils.datetime import now_in_timezone

        today = now_in_timezone(user.timezone).date()
        normalized = date_ref.strip().lower()
        if normalized in {"today", "tonight"}:
            return today
        if normalized == "tomorrow":
            return today + timedelta(days=1)
        return None

    @staticmethod
    def _parse_ai_time(value: object) -> time | None:
        if value is None:
            return None
        match = re.search(r"\b(2[0-3]|[01]?\d)(?::([0-5]\d))?\b", str(value).strip())
        if not match:
            return None
        return time(hour=int(match.group(1)), minute=int(match.group(2) or 0))

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
        existing_notes = conversation.unusual_notes or ""
        note = text.strip()
        duplicate_event = self._find_duplicate_fixed_event(existing_notes=existing_notes, note=note)
        if duplicate_event is not None:
            title, start, end = duplicate_event
            return AssistantResponse(
                intent=AssistantIntent.planning_note,
                text=(
                    f"{title} {start}-{end} is already in the plan for {plan_date.isoformat()}. "
                    "I did not add a duplicate."
                ),
            )
        combined_notes = note
        if existing_notes and note.casefold() not in existing_notes.casefold():
            combined_notes = f"{existing_notes}; {note}"
        await self.planning_repository.save_answer(
            conversation=conversation,
            message_text=text,
            unusual_notes=combined_notes,
            next_state=PlanningConversationState.complete,
        )
        if self._looks_like_activity(text) and not self._looks_like_schedule_note(text):
            cleaned_title = self._clean_task_title(text)
            existing_task = await self.task_repository.find_pending_by_title(
                user_id=user_id,
                title=cleaned_title,
            )
            if existing_task is None:
                await self.task_repository.create_task(
                    user_id=user_id,
                    household_id=household_id,
                    title=cleaned_title,
                    due_date=plan_date,
                )
        plan_text = await self._generate_daily_plan_text(user_id=user_id, plan_date=plan_date)
        return AssistantResponse(
            intent=AssistantIntent.planning_note,
            text=f"Stored as a planning note for {plan_date.isoformat()}.\n\n" + plan_text,
        )

    async def _remove_planning_event(
        self,
        *,
        user_id: UUID,
        title: str,
        plan_date: date | None,
    ) -> AssistantResponse:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before assistant actions can run.")
        from app.utils.datetime import now_in_timezone

        day = plan_date or now_in_timezone(user.timezone).date()
        conversation = await self.planning_repository.get_conversation(user_id=user_id, plan_date=day)
        if conversation is None or not conversation.unusual_notes:
            return AssistantResponse(
                intent=AssistantIntent.unknown,
                text=f"I could not find '{title}' in the plan for {day.isoformat()}.",
            )
        parts = [part.strip() for part in conversation.unusual_notes.split(";") if part.strip()]
        remaining = []
        removed = []
        target = self._normalize_event_title(title)
        for part in parts:
            event = PlanningService._fixed_events_from_notes(part)
            event_title = self._normalize_event_title(event[0]["title"]) if event else ""
            if event and (target in event_title or event_title in target):
                removed.append(part)
                continue
            if target in self._normalize_event_title(part):
                removed.append(part)
                continue
            remaining.append(part)
        if not removed:
            return AssistantResponse(
                intent=AssistantIntent.unknown,
                text=f"I could not find '{title}' in the plan for {day.isoformat()}.",
            )
        await self.planning_repository.save_answer(
            conversation=conversation,
            message_text=f"Removed event: {title}",
            unusual_notes="; ".join(remaining),
            next_state=PlanningConversationState.complete,
        )
        plan_text = await self._generate_daily_plan_text(user_id=user_id, plan_date=day)
        return AssistantResponse(
            intent=AssistantIntent.planning_note,
            text=f"Removed from {day.isoformat()}: {title}.\n\n" + plan_text,
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

    async def _update_sleep_window(
        self,
        *,
        user_id: UUID,
        kind: str,
        value: time,
        plan_date: date | None,
    ) -> AssistantResponse:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before assistant actions can run.")
        household_id = await self._household_id_for_user(user_id=user_id)
        from app.utils.datetime import now_in_timezone

        plan_date = plan_date or now_in_timezone(user.timezone).date()
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
        note = f"{'wake up' if kind == 'wake' else 'go to sleep'} at {value.strftime('%H:%M')}"
        existing_notes = self._replace_sleep_note(conversation.unusual_notes or "", kind, note)
        await self.planning_repository.save_answer(
            conversation=conversation,
            message_text=note,
            unusual_notes=existing_notes,
            next_state=PlanningConversationState.complete,
        )
        plan_text = await self._generate_daily_plan_text(user_id=user_id, plan_date=plan_date)
        return AssistantResponse(
            intent=AssistantIntent.planning_note,
            text=f"Updated {kind} time for {plan_date.isoformat()}.\n\n" + plan_text,
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

        if period in {"today", "tomorrow", "day"}:
            action_tasks = await self._actionable_tasks_for_day(user_id=user_id, day=start)
            return AssistantResponse(
                intent=AssistantIntent.planning_note,
                text=await self._generate_daily_plan_text(user_id=user_id, plan_date=start),
                metadata={
                    "task_actions": [
                        {"id": str(task.id), "title": task.title}
                        for task in action_tasks[:8]
                    ]
                },
            )

        tasks = await self.task_repository.list_pending_for_user(user_id=user_id, through_date=end)
        household_id = await self._household_id_for_user(user_id=user_id)

        if period == "week":
            heading = "This week"
        elif period == "tomorrow":
            heading = "Tomorrow"
        else:
            heading = "Today"
        lines = [heading]
        current = start
        note_lines = []
        while current <= end:
            conversation = await self.planning_repository.get_conversation(
                user_id=user_id,
                plan_date=current,
            )
            if conversation and conversation.unusual_notes:
                note_lines.append(f"- {current.isoformat()}: {conversation.unusual_notes}")
            current += timedelta(days=1)
        if note_lines:
            lines.append("")
            lines.append("Notes")
            lines.extend(note_lines)
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

    async def _actionable_tasks_for_day(self, *, user_id: UUID, day: date) -> list[object]:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before planning can run.")
        household_id = await self._household_id_for_user(user_id=user_id)
        await self.routine_repository.ensure_defaults(household_id=household_id)
        completed_tasks = await self.task_repository.list_completed_for_user_on_date(
            user_id=user_id,
            day=day,
        )
        completed_titles = {task.title.strip().casefold() for task in completed_tasks}
        routines = await self.routine_repository.list_active_for_household(household_id=household_id)
        for routine in routines:
            if routine.title.strip().casefold() in completed_titles:
                continue
            existing_task = await self.task_repository.find_pending_by_title(
                user_id=user_id,
                title=routine.title,
            )
            if existing_task is None:
                await self.task_repository.create_task(
                    user_id=user_id,
                    household_id=household_id,
                    title=routine.title,
                    due_date=day,
                    category="routine",
                )
        return await self.task_repository.list_pending_for_user(user_id=user_id, through_date=day)

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

    async def _update_routine_duration(self, *, user_id: UUID, title: str, minutes: int) -> AssistantResponse:
        household_id = await self._household_id_for_user(user_id=user_id)
        await self.routine_repository.ensure_defaults(household_id=household_id)
        normalized_title = self._normalize_routine_title(title)
        routine = await self.routine_repository.find_by_title(
            household_id=household_id,
            title=normalized_title,
        )
        if routine is None:
            routine = await self.routine_repository.create(
                household_id=household_id,
                title=normalized_title,
                duration_minutes=minutes,
            )
        else:
            routine = await self.routine_repository.update(
                routine=routine,
                title=routine.title,
                duration_minutes=minutes,
            )
        await self.activity_repository.log(
            household_id=household_id,
            user_id=user_id,
            action=ActivityAction.updated,
            entity_type="routine",
            entity_id=routine.id,
            summary=f"Updated must task duration: {routine.title} ({minutes} min)",
        )
        return AssistantResponse(
            intent=AssistantIntent.planning_note,
            text=f"Updated must task: {routine.title} ({minutes} min).",
        )

    async def _mark_task_done(
        self,
        *,
        user_id: UUID,
        title: str,
        completed_on: date | None,
    ) -> AssistantResponse:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before assistant actions can run.")
        from app.utils.datetime import now_in_timezone

        day = completed_on or now_in_timezone(user.timezone).date()
        household_id = await self._household_id_for_user(user_id=user_id)
        cleaned_title = self._clean_task_title(title)
        task = await self.task_repository.mark_pending_by_title_done(
            user_id=user_id,
            household_id=household_id,
            title=cleaned_title,
            completed_on=day,
        )
        if task is None:
            await self.routine_repository.ensure_defaults(household_id=household_id)
            routine = await self.routine_repository.find_by_title(
                household_id=household_id,
                title=self._normalize_routine_title(cleaned_title),
            )
            if routine is not None:
                existing_completed = await self.task_repository.list_completed_for_user_on_date(
                    user_id=user_id,
                    day=day,
                )
                for completed_task in existing_completed:
                    if completed_task.title.strip().casefold() == routine.title.strip().casefold():
                        task = completed_task
                        break
                if task is None:
                    task = await self.task_repository.create_completed_task(
                        user_id=user_id,
                        household_id=household_id,
                        title=routine.title,
                        completed_on=day,
                        category="routine",
                    )
        if task is None:
            return AssistantResponse(
                intent=AssistantIntent.unknown,
                text=f"I could not find a pending task or must task matching '{cleaned_title}'.",
            )
        await self.activity_repository.log(
            household_id=household_id,
            user_id=user_id,
            action=ActivityAction.updated,
            entity_type="task",
            entity_id=task.id,
            summary=f"Marked task done: {task.title}",
        )
        plan_text = await self._generate_daily_plan_text(user_id=user_id, plan_date=day)
        return AssistantResponse(
            intent=AssistantIntent.planning_note,
            text=f"Marked done: {task.title}.\n\nUpdated plan:\n\n" + plan_text,
        )

    async def _apply_task_action_by_title(
        self,
        *,
        user_id: UUID,
        title: str,
        action: str,
        action_date: date | None,
    ) -> AssistantResponse:
        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        if user is None:
            raise RuntimeError("User must exist before assistant actions can run.")
        from app.utils.datetime import now_in_timezone

        day = action_date or now_in_timezone(user.timezone).date()
        household_id = await self._household_id_for_user(user_id=user_id)
        cleaned_title = self._clean_task_title(title)
        task = await self.task_repository.apply_action_by_title(
            user_id=user_id,
            household_id=household_id,
            title=cleaned_title,
            action=action,
            today=day,
        )
        if task is None and action == "done":
            return await self._mark_task_done(
                user_id=user_id,
                title=cleaned_title,
                completed_on=day,
            )
        if task is None:
            return AssistantResponse(
                intent=AssistantIntent.unknown,
                text=f"I could not find a pending task matching '{cleaned_title}'.",
            )
        action_text = {
            "done": "Marked done",
            "skip": "Skipped",
            "move": "Moved to tomorrow",
        }[action]
        await self.activity_repository.log(
            household_id=household_id,
            user_id=user_id,
            action=ActivityAction.updated,
            entity_type="task",
            entity_id=task.id,
            summary=f"{action_text}: {task.title}",
        )
        plan_date = day + timedelta(days=1) if action == "move" else day
        plan_text = await self._generate_daily_plan_text(user_id=user_id, plan_date=plan_date)
        return AssistantResponse(
            intent=AssistantIntent.planning_note,
            text=f"{action_text}: {task.title}.\n\nUpdated plan:\n\n" + plan_text,
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
        await self.routine_repository.ensure_defaults(household_id=household_id)
        routines = await self.routine_repository.list_active_for_household(household_id=household_id)
        completed_tasks = await self.task_repository.list_completed_for_user_on_date(
            user_id=user_id,
            day=plan_date,
        )
        completed_titles = {task.title.strip().casefold() for task in completed_tasks}
        calendar_events = await CalendarService(self.session).list_events_for_day(
            household_id=household_id,
            day=plan_date,
            timezone=user.timezone,
        )
        from app.utils.datetime import now_in_timezone

        today = now_in_timezone(user.timezone).date()
        current_time = now_in_timezone(user.timezone).time() if plan_date == today else None

        planning_service = PlanningService()
        planned_tasks: list[str | PlannedTaskInput] = []
        for routine in routines:
            if routine.title.strip().casefold() in completed_titles:
                continue
            schedule = routine.schedule or {}
            planned_tasks.append(
                PlannedTaskInput(
                    title=routine.title,
                    duration_minutes=int(
                        schedule.get("duration_minutes")
                        or schedule.get("duration_min")
                        or 30
                    ),
                    must=bool(schedule.get("must", True)),
                )
            )
        planned_tasks.extend(task.title for task in tasks)
        plan = planning_service.build_daily_plan(
            PlanningInput(
                user_id=user_id,
                plan_date=plan_date,
                work_start=conversation.work_start if conversation else None,
                work_end=conversation.work_end if conversation else None,
                unusual_notes=conversation.unusual_notes if conversation else None,
                tasks=planned_tasks,
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

        non_supermarket: dict[str, list[str]] = {}
        by_store: dict[str, list[str]] = {}
        for item in items:
            category = self.shopping_category_service.category_for(
                item.name, store_name_raw=item.store_name_raw
            )
            if category == "Supermarket":
                store_key = item.store_name_raw.strip().title() if item.store_name_raw else "Anywhere"
                by_store.setdefault(store_key, []).append(f"- {item.name}")
            else:
                non_supermarket.setdefault(category, []).append(
                    f"- {item.name} ({item.store_name_raw or 'anywhere'})"
                )

        lines = ["Shopping list:"]
        for category in ("Online", "Health", "Tech", "Clothes", "House"):
            category_items = non_supermarket.pop(category, [])
            if not category_items:
                continue
            lines.append("")
            lines.append(category)
            lines.extend(category_items)
        for category, category_items in sorted(non_supermarket.items()):
            lines.append("")
            lines.append(category)
            lines.extend(category_items)

        known_stores = ("Continente", "Lidl", "Aldi", "Pingo Doce", "Mercadona", "Mini Mix")
        for store in known_stores:
            store_items = by_store.pop(store, [])
            if not store_items:
                continue
            lines.append("")
            lines.append(store)
            lines.extend(store_items)
        anywhere_items = by_store.pop("Anywhere", [])
        for store, store_items in sorted(by_store.items()):
            lines.append("")
            lines.append(store)
            lines.extend(store_items)
        if anywhere_items:
            lines.append("")
            lines.append("Anywhere")
            lines.extend(anywhere_items)

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
                for item in removed_items:
                    await self.activity_repository.log(
                        household_id=household_id,
                        user_id=user_id,
                        action=ActivityAction.deleted,
                        entity_type="shopping_item",
                        entity_id=item.id,
                        summary=f"Removed shopping item: {item.name}",
                    )
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
                await self.activity_repository.log(
                    household_id=household_id,
                    user_id=user_id,
                    action=ActivityAction.deleted,
                    entity_type="task",
                    entity_id=removed_task.id,
                    summary=f"Removed task: {removed_task.title}",
                )
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
            await self.activity_repository.log(
                household_id=household_id,
                user_id=user_id,
                action=ActivityAction.updated,
                entity_type="task",
                entity_id=moved.id,
                summary=f"Rescheduled task to {moved.due_date.isoformat()}: {moved.title}",
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
            await self.activity_repository.log(
                household_id=household_id,
                user_id=user_id,
                action=ActivityAction.updated,
                entity_type="task",
                entity_id=created.id,
                summary=f"Moved shopping item to tasks: {created.title}",
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
            await self.activity_repository.log(
                household_id=household_id,
                user_id=user_id,
                action=ActivityAction.updated,
                entity_type="shopping_item",
                entity_id=saved.id,
                summary=f"Moved task to shopping list: {saved.name}",
            )
            return AssistantResponse(
                intent=AssistantIntent.item_moved,
                text=f"Moved from tasks to shopping list: {saved.name}.",
            )

        # Treat target as a store name to reassign on the shopping list
        _anywhere_aliases = {"anywhere", "qualquer sitio", "qualquer sítio", "qualquer loja"}
        new_store = None if target.lower() in _anywhere_aliases else target
        reassigned = await self.shopping_repository.reassign_store(
            household_id=household_id,
            item_name=name,
            new_store=new_store,
        )
        if reassigned is None:
            return AssistantResponse(
                intent=AssistantIntent.unknown,
                text=f"I could not find '{name}' on the shopping list.",
            )
        store_label = new_store or "anywhere"
        await self.activity_repository.log(
            household_id=household_id,
            user_id=user_id,
            action=ActivityAction.updated,
            entity_type="shopping_item",
            entity_id=reassigned.id,
            summary=f"Reassigned shopping item store: {reassigned.name} → {store_label}",
        )
        return AssistantResponse(
            intent=AssistantIntent.item_moved,
            text=f"Moved {reassigned.name} to {store_label}.",
        )

    async def _expense_summary(
        self,
        *,
        user_id: UUID,
        period: str,
        store_name: str | None,
        query_kind: str = "spend",
        category: str | None = None,
        original_question: str | None = None,
    ) -> AssistantResponse:
        from app.utils.datetime import now_in_timezone

        user = await UserRepository(self.session).get_by_id(user_id=user_id)
        today = now_in_timezone(user.timezone if user else "Europe/Lisbon").date()
        household_id = await self._household_id_for_user(user_id=user_id)

        # For general grocery spend (no category, no income), use the structured path
        if query_kind == "spend" and not category:
            analytics = AnalyticsService(self.session)
            text = await analytics.grocery_spend_summary(
                household_id=household_id,
                today=today,
                period=period,
                store_name=store_name,
            )
            return AssistantResponse(intent=AssistantIntent.expense_summary, text=text)

        # For category or income queries, feed raw data rows to the AI
        analytics = AnalyticsService(self.session)
        start_date, end_date = analytics._date_range(today=today, period=period)

        finance_repo = FinanceRepository(self.session)
        transactions = await finance_repo.list_between(
            household_id=household_id,
            start_date=start_date,
            end_date=end_date,
        )

        receipt_repo = ReceiptRepository(self.session)
        receipts = await receipt_repo.list_receipts_between(
            household_id=household_id,
            start_date=start_date,
            end_date=end_date,
        )

        tx_rows = [
            {
                "date": str(t.occurred_on),
                "type": t.transaction_type.value if hasattr(t.transaction_type, "value") else str(t.transaction_type),
                "category": t.category or "",
                "description": t.description or "",
                "amount": t.amount or "0",
                "currency": t.currency or "EUR",
            }
            for t in transactions
        ]

        item_rows = [
            {
                "date": str(receipt_repo.effective_receipt_date(r)),
                "store": r.shop_name or "Unknown",
                "name": item.name or "",
                "amount": item.total_amount or "0",
            }
            for r in receipts
            for item in r.items
        ]

        question = original_question or (
            f"How much did we spend on {category} during {period}?"
            if category
            else f"What was the income for {period}?"
        )

        text = await self.ai_router.answer_finance_question(
            question=question,
            tx_rows=tx_rows,
            item_rows=item_rows,
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
        if lowered.startswith(("need ", "i need ", "we need ", "need to ", "i need to ", "we need to ")):
            return None
        if AssistantService._parse_planning_query(stripped) is not None:
            return None
        patterns = (
            r"^(?:task|todo|to do):\s+(.+)$",
            r"^(?:task|todo|to do)\s+(.+)$",
            r"^(?:add task|create task|remind me to)\s+(.+)$",
            r"^(?:can you remind me to|please remind me to)\s+(.+)$",
            r"^(?:i want to|we want to|i would like to|we would like to)\s+(.+)$",
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
        match = re.search(r"\b(2[0-3]|[01]?\d)(?:(?::|\.|h)([0-5]\d))?\b", lowered)
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
        matches = list(re.finditer(r"\b(2[0-3]|[01]?\d)(?:(?::|\.|h)([0-5]\d))?\b", lowered))
        if len(matches) < 2:
            return None
        start_match, end_match = matches[0], matches[1]
        start = time(hour=int(start_match.group(1)), minute=int(start_match.group(2) or 0))
        end = time(hour=int(end_match.group(1)), minute=int(end_match.group(2) or 0))
        return start, end, None

    @staticmethod
    def _parse_sleep_window_update(text: str) -> tuple[str, time, date | None] | None:
        lowered = text.lower()
        if not re.search(r"\b(wake(?:\s+up)?|sleep|bed)\b", lowered):
            return None
        match = re.search(
            r"\b(?P<kind>wake(?:\s+up)?|sleep|go\s+to\s+sleep|bed)\b(?:\s+at|\s+around|\s+by)?\s+(?P<hour>2[0-3]|[01]?\d)(?:(?::|\.|h)(?P<minute>[0-5]\d))?\b",
            lowered,
        )
        if not match:
            return None
        kind = "wake" if "wake" in match.group("kind") else "sleep"
        parsed_time = time(
            hour=int(match.group("hour")),
            minute=int(match.group("minute") or 0),
        )
        date_ref = AssistantService._date_from_text(text, "Europe/Lisbon")
        return kind, parsed_time, date_ref

    @staticmethod
    def _replace_sleep_note(existing_notes: str, kind: str, note: str) -> str:
        parts = [
            part.strip()
            for part in existing_notes.split(";")
            if part.strip()
        ]
        if kind == "wake":
            parts = [part for part in parts if not re.search(r"\bwake(?:\s+up)?\b", part, flags=re.IGNORECASE)]
        else:
            parts = [
                part
                for part in parts
                if not re.search(r"\b(?:sleep|go\s+to\s+sleep|bed)\b", part, flags=re.IGNORECASE)
            ]
        parts.append(note)
        return "; ".join(parts)

    @staticmethod
    def _tomorrow_for_user_timezone(timezone: str) -> date:
        from app.utils.datetime import now_in_timezone

        return now_in_timezone(timezone).date() + timedelta(days=1)

    @staticmethod
    def _parse_expense_query(text: str) -> tuple[str, str | None, str, str | None] | None:
        lowered = text.lower().strip(" ?!.")
        _INCOME_WORDS = ("income", " earn", "earned", "salary", "salário", "salario", "ordenado")
        _SPEND_WORDS = ("spend", "spent", "expense", "how much", "cost", "groceries")
        is_income = any(w in lowered for w in _INCOME_WORDS)
        is_spend = any(w in lowered for w in _SPEND_WORDS)
        if not is_income and not is_spend:
            return None
        query_kind = "income" if is_income else "spend"

        _MONTHS: dict[str, int] = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }
        period = "this month"
        if "last year" in lowered:
            period = "last year"
        elif "this year" in lowered:
            period = "this year"
        elif "last month" in lowered:
            period = "last month"
        elif "this week" in lowered:
            period = "this week"
        elif "this month" in lowered:
            period = "this month"
        else:
            m_named = re.search(
                r"\bin\s+(" + "|".join(_MONTHS.keys()) + r")\s*(?:(20\d{2}))?\b",
                lowered,
            )
            if m_named:
                month_num = _MONTHS[m_named.group(1)]
                year = int(m_named.group(2)) if m_named.group(2) else date.today().year
                period = f"month:{year:04d}-{month_num:02d}"
            else:
                m_year = re.search(r"\bin\s*(20\d{2})\b|\b(20\d{2})\b", lowered)
                if m_year:
                    period = f"year:{int(m_year.group(1) or m_year.group(2))}"

        # Remove period phrases so they don't bleed into category extraction
        _period_pat = (
            r"\b(this month|this week|this year|last month|last year"
            r"|in\s+(?:" + "|".join(_MONTHS.keys()) + r")(?:\s+20\d{2})?"
            r"|in\s+20\d{2}|20\d{2})\b"
        )
        clean = re.sub(_period_pat, "", lowered, flags=re.IGNORECASE).strip(" ?.")

        category: str | None = None
        cat_match = re.search(
            r"\b(?:spend(?:ing)?|spent|expenses?)\s+on\s+([a-zA-ZÀ-ÿ][\w\s'&-]{1,30})",
            clean,
        ) or re.search(
            r"\bon\s+([a-zA-ZÀ-ÿ][\w\s'&-]{1,30})",
            clean,
        )
        if cat_match:
            cat = cat_match.group(1).strip()
            _skip = {"average", "the", "a", "an", "us", "we"}
            if len(cat) > 1 and cat.lower() not in _skip:
                category = cat

        store_name: str | None = None
        if query_kind == "spend" and not category:
            store_match = re.search(r"\b(?:at|from)\s+([a-zA-ZÀ-ÿ0-9' -]+)", text)
            if store_match:
                store_name = store_match.group(1).strip(" ?.")
                for suffix in (" this week", " this month", " week", " month", " year"):
                    if store_name.lower().endswith(suffix):
                        store_name = store_name[: -len(suffix)].strip()
        return period, store_name, query_kind, category

    @staticmethod
    def _period_from_date_ref(date_ref: str) -> str:
        dr = date_ref.strip().lower()
        if dr in {"", "today", "tonight"}:
            return "this month"
        if dr == "week":
            return "this week"
        if dr in {"this month", "month"}:
            return "this month"
        if dr == "last month":
            return "last month"
        if dr in {"this year", "year"}:
            return "this year"
        if dr == "last year":
            return "last year"
        _MONTHS: dict[str, int] = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }
        m = re.match(r"^(" + "|".join(_MONTHS.keys()) + r")(?:\s+(20\d{2}))?$", dr)
        if m:
            month_num = _MONTHS[m.group(1)]
            year = int(m.group(2)) if m.group(2) else date.today().year
            return f"month:{year:04d}-{month_num:02d}"
        m_year = re.match(r"^(20\d{2})$", dr)
        if m_year:
            return f"year:{m_year.group(1)}"
        return "this month"

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
            "what to do",
            "what do i do",
            "what should be done",
            "what should i do",
            "what should we do",
            "what needs doing",
            "what do i need to do",
            "what do we need to do",
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
        if match:
            content = match.group(1).strip()
            # Time references are queries, not notes
            if re.match(
                r"^(today|tonight|tomorrow|this week|next week|for today|for tomorrow|for this week)$",
                content,
                flags=re.IGNORECASE,
            ):
                return None
            return content, None
        lowered = stripped.lower()
        if re.search(r"\b(?:go to sleep|sleep|bed|wake up)\b", lowered) and re.search(
            r"\b(?:at\s*)?(2[0-3]|[01]?\d)(?:(?::|\.|h)([0-5]\d))?\b",
            lowered,
        ):
            return stripped, None
        return None

    @staticmethod
    def _parse_remove_event_request(text: str) -> tuple[str, date | None] | None:
        stripped = text.strip()
        lowered = stripped.lower()
        if not any(word in lowered for word in ("remove", "delete", "cancel")):
            return None
        if not any(word in lowered for word in ("event", "plan", "wedding", "meeting", "appointment", "church", "party")):
            return None
        match = re.match(
            r"^(?:remove|delete|cancel)\s+(.+?)(?:\s+from\s+(?:the\s+)?(?:plan|events?))?(?:\s+(today|tomorrow|tonight))?$",
            stripped,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        title = re.sub(
            r"\b(?:event|appointment|from plan|from the plan)\b",
            "",
            match.group(1),
            flags=re.IGNORECASE,
        ).strip(" .")
        if not title:
            return None
        return title, AssistantService._date_from_text(stripped, "Europe/Lisbon")

    @staticmethod
    def _parse_fixed_event_note(text: str) -> tuple[str, date | None] | None:
        stripped = text.strip()
        lowered = stripped.lower()
        if not re.search(
            r"\b(2[0-3]|[01]?\d)(?:(?::|\.|h)([0-5]\d))?\s*(?:to|and|-)\s*(2[0-3]|[01]?\d)(?:(?::|\.|h)([0-5]\d))?\b",
            lowered,
        ):
            return None
        event_markers = (
            "change of plans",
            "today is",
            "tomorrow is",
            "tonight is",
            "i have",
            "we have",
            "there is",
            "there's",
            "appointment",
            "wedding",
            "event",
            "dinner",
            "meeting",
            "church",
            "party",
        )
        if not any(marker in lowered for marker in event_markers):
            return None
        return stripped, AssistantService._date_from_text(stripped, "Europe/Lisbon")

    @staticmethod
    def _find_duplicate_fixed_event(
        *,
        existing_notes: str,
        note: str,
    ) -> tuple[str, str, str] | None:
        new_events = PlanningService._fixed_events_from_notes(note)
        existing_events = PlanningService._fixed_events_from_notes(existing_notes)
        for new_event in new_events:
            new_key = (
                AssistantService._normalize_event_title(new_event["title"]),
                new_event["start"],
                new_event["end"],
            )
            for existing_event in existing_events:
                existing_key = (
                    AssistantService._normalize_event_title(existing_event["title"]),
                    existing_event["start"],
                    existing_event["end"],
                )
                if new_key == existing_key:
                    return new_event["title"], new_event["start"], new_event["end"]
        return None

    @staticmethod
    def _normalize_event_title(title: str) -> str:
        normalized = re.sub(r"[^a-z0-9 ]+", " ", title.lower())
        words = [
            word
            for word in normalized.split()
            if word not in {"the", "a", "an", "today", "tomorrow", "tonight", "event"}
        ]
        return " ".join(words)

    @staticmethod
    def _parse_task_completion(text: str) -> tuple[str, date | None] | None:
        stripped = text.strip()
        lowered = stripped.lower()
        completion_markers = (
            "already",
            "done",
            "finished",
            "completed",
            "did",
            "i read",
            "we read",
            "i exercised",
            "we exercised",
            "tick",
            "mark",
        )
        if not any(marker in lowered for marker in completion_markers):
            return None
        patterns = (
            r"^(?:i|we)\s+already\s+(.+?)(?:\s+(today|tomorrow|yesterday))?$",
            r"^(?:i|we)\s+(?:finished|completed|did)\s+(.+?)(?:\s+(today|tomorrow|yesterday))?$",
            r"^(?:done|finished|completed)\s+(.+?)(?:\s+(today|tomorrow|yesterday))?$",
            r"^(?:mark|tick)\s+(.+?)\s+(?:as\s+)?(?:done|completed)(?:\s+(today|tomorrow|yesterday))?$",
            r"^(?:i|we)\s+read\s+(.+?)(?:\s+(today|tomorrow|yesterday))?$",
            r"^(?:i|we)\s+exercised(?:\s+(today|tomorrow|yesterday))?$",
        )
        for pattern in patterns:
            match = re.match(pattern, stripped, flags=re.IGNORECASE)
            if not match:
                continue
            if "exercised" in pattern:
                title = "exercise"
            else:
                title = match.group(1).strip(" .")
            if title.lower().startswith(("the ", "a ", "an ")):
                title = title.split(" ", 1)[1]
            title = AssistantService._normalize_routine_title(title)
            date_ref = AssistantService._date_from_text(stripped, "Europe/Lisbon")
            return title, date_ref
        return None

    @staticmethod
    def _parse_task_action(text: str) -> tuple[str, str, date | None] | None:
        stripped = text.strip()
        patterns = (
            ("skip", r"^(?:skip|skipped)\s+(.+?)(?:\s+(today|tomorrow|yesterday))?$"),
            ("move", r"^(?:move|reschedule)\s+(.+?)\s+(?:to|for)\s+tomorrow$"),
            ("move", r"^move\s+(.+?)\s+tomorrow$"),
        )
        for action, pattern in patterns:
            match = re.match(pattern, stripped, flags=re.IGNORECASE)
            if not match:
                continue
            title = match.group(1).strip(" .")
            if title.lower().startswith(("the ", "a ", "an ")):
                title = title.split(" ", 1)[1]
            title = AssistantService._normalize_routine_title(title)
            date_ref = AssistantService._date_from_text(stripped, "Europe/Lisbon")
            return title, action, date_ref
        return None

    @staticmethod
    def _parse_conversational(text: str) -> AssistantResponse | None:
        lowered = re.sub(r"\s+", " ", text.lower().strip(" ?!."))
        if lowered in {"hi", "hello", "hey", "hiya", "good morning", "good evening", "good afternoon"}:
            return AssistantResponse(
                intent=AssistantIntent.unknown,
                text="Hi. I can help with plans, tasks, shopping, receipts, and expenses.",
            )
        if lowered in {"how are you", "how are you doing", "how's it going", "how are u"}:
            return AssistantResponse(
                intent=AssistantIntent.unknown,
                text="I am running normally. Send me a plan change, task, shopping item, receipt, or expense.",
            )
        if lowered in {"what are you", "who are you", "what is this", "what can you do", "help", "what do you do"}:
            return AssistantResponse(
                intent=AssistantIntent.unknown,
                text=(
                    "I am your household assistant. I can plan your day, update changed plans, "
                    "track tasks, manage shopping, read receipts, and summarize expenses."
                ),
            )
        if lowered in {"thanks", "thank you", "thx"}:
            return AssistantResponse(intent=AssistantIntent.unknown, text="You are welcome.")
        return None

    @staticmethod
    def _parse_routine_duration_update(text: str) -> tuple[str, int] | None:
        stripped = text.strip()
        lowered = stripped.lower()
        if not re.search(r"\b(?:min|mins|minute|minutes)\b", lowered):
            return None
        patterns = (
            r"^(?:change|set|make|update)\s+(.+?)\s+(?:to\s+)?(\d{1,3})\s*(?:min|mins|minute|minutes)\b",
            r"^(.+?)\s+(\d{1,3})\s*(?:min|mins|minute|minutes)\b",
        )
        for pattern in patterns:
            match = re.match(pattern, stripped, flags=re.IGNORECASE)
            if not match:
                continue
            title = AssistantService._normalize_routine_title(match.group(1))
            minutes = int(match.group(2))
            if not title or minutes < 1 or minutes > 360:
                return None
            if title in {"read the bible", "exercise"} or any(
                marker in lowered for marker in ("daily", "routine", "must task")
            ):
                return title, minutes
        return None

    @staticmethod
    def _normalize_routine_title(title: str) -> str:
        normalized = title.strip(" .").lower()
        normalized = re.sub(r"^(?:daily|routine|must task|task)\s+", "", normalized)
        normalized = normalized.replace("exercice", "exercise")
        normalized = normalized.replace("workout", "exercise")
        if normalized in {"bible", "the bible"}:
            normalized = "read the bible"
        normalized = normalized.replace("read bible", "read the bible")
        normalized = normalized.replace("bible reading", "read the bible")
        return normalized

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
        # Store reassignment: "move beef to Lidl", "move lentils to anywhere"
        _reserved_targets = {
            "tomorrow", "today", "tonight", "this week", "next week",
            "shopping", "shopping list", "task", "tasks", "plan",
        }
        match = re.match(
            r"^(?:move|change|put|reassign)\s+(.+?)\s+(?:to|para(?:\s+(?:o|a|os|as))?)\s+(.+)$",
            stripped,
            flags=re.IGNORECASE,
        )
        if match:
            name_part = match.group(1).strip(" .")
            target_part = match.group(2).strip(" .")
            if target_part.lower() not in _reserved_targets:
                return {"name": name_part, "target": target_part, "date_text": ""}
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
        "play",
        "practice",
        "watch",
        "read",
        "sleep",
        "nap",
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

    @staticmethod
    def _looks_like_activity(text: str) -> bool:
        lowered = text.lower()
        return any(verb in lowered for verb in AssistantService._ACTION_VERBS)

    @staticmethod
    def _looks_like_schedule_note(text: str) -> bool:
        lowered = text.lower()
        return bool(
            re.search(r"\b(?:go to sleep|sleep|bed|wake up)\b", lowered)
            and re.search(r"\b(?:at\s*)?(2[0-3]|[01]?\d)(?:(?::|\.|h)([0-5]\d))?\b", lowered)
        )
