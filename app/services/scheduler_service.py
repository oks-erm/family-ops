import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.bot.keyboards import task_action_keyboard
from app.config import Settings
from app.db.models import DailyPlanStatus, PlanningConversation, PlanningConversationState
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.planning import PlanningRepository
from app.db.repositories.routines import RoutineRepository
from app.db.repositories.tasks import TaskRepository
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory
from app.services.analytics_service import AnalyticsService
from app.services.calendar_service import CalendarService
from app.services.planning_service import PlannedTaskInput, PlanningInput, PlanningService
from app.services.price_service import PriceService
from app.services.recommendation_service import RecommendationService

logger = logging.getLogger(__name__)


class SchedulerService:
    def __init__(self, *, settings: Settings, bot: Bot | None) -> None:
        self.settings = settings
        self.bot = bot
        self.scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.default_timezone))

    def start(self) -> None:
        self.scheduler.add_job(
            self.run_evening_planning_prompts,
            CronTrigger(minute="*", timezone=ZoneInfo(self.settings.default_timezone)),
            id="evening_planning_prompts",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.run_morning_plans,
            CronTrigger(minute="*", timezone=ZoneInfo(self.settings.default_timezone)),
            id="morning_plans",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.run_evening_reviews,
            CronTrigger(minute="*", timezone=ZoneInfo(self.settings.default_timezone)),
            id="evening_reviews",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.sync_calendars,
            CronTrigger(minute="*/5", timezone=ZoneInfo(self.settings.default_timezone)),
            id="sync_calendars",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.cleanup_stale_planning_prompts,
            CronTrigger(minute="*/30", timezone=ZoneInfo(self.settings.default_timezone)),
            id="cleanup_stale_planning_prompts",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.refresh_prices,
            CronTrigger(hour=7, minute=30, timezone=ZoneInfo(self.settings.default_timezone)),
            id="refresh_prices",
            replace_existing=True,
        )
        weekly_hour, weekly_minute = self._parse_time(self.settings.weekly_recommendation_time)
        self.scheduler.add_job(
            self.generate_weekly_recommendations,
            CronTrigger(
                day_of_week="mon",
                hour=weekly_hour,
                minute=weekly_minute,
                timezone=ZoneInfo(self.settings.default_timezone),
            ),
            id="weekly_recommendations",
            replace_existing=True,
        )
        monthly_hour, monthly_minute = self._parse_time(self.settings.monthly_summary_time)
        self.scheduler.add_job(
            self.send_month_end_summary,
            CronTrigger(
                day="last",
                hour=monthly_hour,
                minute=monthly_minute,
                timezone=ZoneInfo(self.settings.default_timezone),
            ),
            id="month_end_summary",
            replace_existing=True,
        )
        self.scheduler.start()
        logger.info("Scheduler started for planning, review, calendar sync, recommendations, and summaries.")

    async def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def run_evening_planning_prompts(self) -> None:
        if self.bot is None:
            return
        async with async_session_factory() as session:
            users = await UserRepository(session).list_users()
            household_repository = HouseholdRepository(session)
            planning_repository = PlanningRepository(session)
            for user in users:
                if not self._is_user_time(user.timezone, self.settings.planning_evening_time):
                    continue
                try:
                    household = await household_repository.ensure_household_for_user(user=user)
                    plan_date = self._today(user.timezone) + timedelta(days=1)
                    existing_conversation = await planning_repository.get_conversation(
                        user_id=user.id,
                        plan_date=plan_date,
                    )
                    conversation = existing_conversation
                    if conversation is None:
                        conversation = await planning_repository.start_conversation(
                            user_id=user.id,
                            household_id=household.id,
                            plan_date=plan_date,
                        )
                    prompt_key = None
                    prompt_text = None
                    next_state = None
                    if conversation.work_start is None:
                        prompt_key = f"scheduled_prompt:work_start:{plan_date.isoformat()}"
                        prompt_text = "What time do you start work tomorrow?"
                        next_state = PlanningConversationState.awaiting_work_start
                    elif conversation.work_end is None:
                        prompt_key = f"scheduled_prompt:work_end:{plan_date.isoformat()}"
                        prompt_text = "What time do you finish work tomorrow?"
                        next_state = PlanningConversationState.awaiting_work_end
                    elif not conversation.unusual_notes:
                        prompt_key = f"scheduled_prompt:unusual:{plan_date.isoformat()}"
                        prompt_text = (
                            "Anything unusual tomorrow? Appointments, errands, low energy, "
                            "church, workout?"
                        )
                        next_state = PlanningConversationState.awaiting_unusual_notes
                    if prompt_text is None or prompt_key is None or next_state is None:
                        continue
                    if self._prompt_was_sent(conversation.raw_notes or [], prompt_key):
                        continue
                    conversation.state = next_state
                    conversation.raw_notes = [
                        *(conversation.raw_notes or []),
                        {
                            "state": "scheduled",
                            "text": prompt_key,
                            "sent_at": datetime.now(UTC).isoformat(),
                        },
                    ]
                    await session.commit()
                    sent_message = await self.bot.send_message(chat_id=user.telegram_chat_id, text=prompt_text)
                    conversation.raw_notes = [
                        *(conversation.raw_notes or []),
                        {
                            "state": "scheduled_message",
                            "text": prompt_key,
                            "chat_id": user.telegram_chat_id,
                            "message_id": sent_message.message_id,
                            "sent_at": datetime.now(UTC).isoformat(),
                        },
                    ]
                    await session.commit()
                except Exception:
                    logger.exception("Failed to send evening planning prompt to user %s", user.id)

    async def run_morning_plans(self) -> None:
        if self.bot is None:
            return
        async with async_session_factory() as session:
            users = await UserRepository(session).list_users()
            for user in users:
                if not self._is_user_time(user.timezone, self.settings.morning_plan_time):
                    continue
                try:
                    await self._send_or_create_morning_plan(session=session, user=user)
                except Exception:
                    logger.exception("Failed to send morning plan to user %s", user.id)

    async def run_evening_reviews(self) -> None:
        if self.bot is None:
            return
        async with async_session_factory() as session:
            users = await UserRepository(session).list_users()
            household_repository = HouseholdRepository(session)
            planning_repository = PlanningRepository(session)
            task_repository = TaskRepository(session)
            for user in users:
                if not self._is_user_time(user.timezone, self.settings.evening_review_time):
                    continue
                try:
                    await household_repository.ensure_household_for_user(user=user)
                    today = self._today(user.timezone)
                    plan = await planning_repository.get_daily_plan(user_id=user.id, plan_date=today)
                    if plan is None or plan.status != DailyPlanStatus.sent:
                        continue
                    tasks = await task_repository.list_pending_for_user(
                        user_id=user.id,
                        through_date=today,
                    )
                    if tasks:
                        await self.bot.send_message(
                            chat_id=user.telegram_chat_id,
                            text="Evening review: tap what happened today.",
                        )
                        for task in tasks:
                            await self.bot.send_message(
                                chat_id=user.telegram_chat_id,
                                text=task.title,
                                reply_markup=task_action_keyboard(str(task.id)),
                            )
                    else:
                        logger.info("Skipping empty evening review for user %s.", user.id)
                    plan.status = DailyPlanStatus.reviewed
                    await session.commit()
                except Exception:
                    logger.exception("Failed to send evening review to user %s", user.id)

    async def cleanup_stale_planning_prompts(self) -> None:
        if self.bot is None:
            return
        cutoff = datetime.now(UTC) - timedelta(hours=6)
        async with async_session_factory() as session:
            result = await session.execute(
                select(PlanningConversation).where(
                    PlanningConversation.state != PlanningConversationState.complete,
                    PlanningConversation.updated_at < cutoff,
                )
            )
            conversations = list(result.scalars().all())
            for conversation in conversations:
                try:
                    for note in conversation.raw_notes or []:
                        if not isinstance(note, dict) or note.get("state") != "scheduled_message":
                            continue
                        chat_id = note.get("chat_id")
                        message_id = note.get("message_id")
                        if chat_id is None or message_id is None:
                            continue
                        try:
                            await self.bot.delete_message(chat_id=int(chat_id), message_id=int(message_id))
                        except Exception:
                            logger.info("Could not delete stale planning prompt %s.", message_id)
                    conversation.state = PlanningConversationState.complete
                    conversation.raw_notes = [
                        *(conversation.raw_notes or []),
                        {
                            "state": "expired",
                            "text": "Planning prompt expired after 6 hours without response.",
                            "expired_at": datetime.now(UTC).isoformat(),
                        },
                    ]
                    await session.commit()
                except Exception:
                    logger.exception("Failed to expire stale planning conversation %s", conversation.id)

    async def sync_calendars(self) -> None:
        async with async_session_factory() as session:
            service = CalendarService(session)
            try:
                await service.sync_ical_feeds()
            except Exception:
                logger.exception("iCal sync failed.")
            try:
                await service.sync_google_connections()
            except Exception:
                logger.exception("Google Calendar sync failed.")

    async def refresh_prices(self) -> None:
        async with async_session_factory() as session:
            users = await UserRepository(session).list_users()
            household_repository = HouseholdRepository(session)
            seen_households = set()
            for user in users:
                try:
                    household = await household_repository.ensure_household_for_user(user=user)
                    if household.id in seen_households:
                        continue
                    seen_households.add(household.id)
                    await PriceService(session).refresh_shopping_prices(household_id=household.id)
                except Exception:
                    logger.exception("Failed to refresh prices for user %s", user.id)

    async def generate_weekly_recommendations(self) -> None:
        async with async_session_factory() as session:
            users = await UserRepository(session).list_users()
            household_repository = HouseholdRepository(session)
            recommendation_service = RecommendationService(session)
            seen_households = set()
            today = self._today(self.settings.default_timezone)
            for user in users:
                try:
                    household = await household_repository.ensure_household_for_user(user=user)
                    if household.id in seen_households:
                        continue
                    seen_households.add(household.id)
                    await recommendation_service.generate_weekly_for_household(
                        household_id=household.id,
                        today=today,
                    )
                except Exception:
                    logger.exception("Failed to generate weekly recommendation for user %s", user.id)

    async def _send_or_create_morning_plan(self, *, session, user) -> None:
        household_repository = HouseholdRepository(session)
        planning_repository = PlanningRepository(session)
        task_repository = TaskRepository(session)
        routine_repository = RoutineRepository(session)
        household = await household_repository.ensure_household_for_user(user=user)
        today = self._today(user.timezone)
        existing_plan = await planning_repository.get_daily_plan(user_id=user.id, plan_date=today)
        if existing_plan is not None and existing_plan.status != DailyPlanStatus.draft:
            return

        conversation = await planning_repository.get_conversation(user_id=user.id, plan_date=today)
        await routine_repository.ensure_defaults(household_id=household.id)
        routines = await routine_repository.list_active_for_household(household_id=household.id)
        completed_today = await task_repository.list_completed_for_user_on_date(
            user_id=user.id,
            day=today,
        )
        completed_titles = {task.title.strip().casefold() for task in completed_today}
        for routine in routines:
            if routine.title.strip().casefold() in completed_titles:
                continue
            existing_task = await task_repository.find_pending_by_title(
                user_id=user.id,
                title=routine.title,
            )
            if existing_task is None:
                await task_repository.create_task(
                    user_id=user.id,
                    household_id=household.id,
                    title=routine.title,
                    due_date=today,
                    category="routine",
                )
        tasks = await task_repository.list_pending_for_user(
            user_id=user.id,
            through_date=today,
        )
        calendar_events = await CalendarService(session).list_events_for_day(
            household_id=household.id,
            day=today,
            timezone=user.timezone,
        )
        planned_tasks: list[str | PlannedTaskInput] = []
        for routine in routines:
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
        planned_tasks.extend(task.title for task in tasks if task.category != "routine")
        planning_service = PlanningService()
        plan_payload = planning_service.build_daily_plan(
            PlanningInput(
                user_id=user.id,
                plan_date=today,
                work_start=conversation.work_start if conversation else None,
                work_end=conversation.work_end if conversation else None,
                unusual_notes=conversation.unusual_notes if conversation else None,
                tasks=planned_tasks,
                calendar_events=calendar_events,
            )
        )
        existing_plan = await planning_repository.upsert_daily_plan(
            user_id=user.id,
            household_id=household.id,
            plan_date=today,
            work_start=conversation.work_start if conversation else None,
            work_end=conversation.work_end if conversation else None,
            unusual_notes=conversation.unusual_notes if conversation else None,
            plan=plan_payload,
            status=DailyPlanStatus.draft,
        )

        planning_service = PlanningService()
        await self.bot.send_message(
            chat_id=user.telegram_chat_id,
            text=planning_service.render_morning_brief(existing_plan.plan),
        )
        existing_plan.status = DailyPlanStatus.sent
        await session.commit()

    async def send_month_end_summary(self) -> None:
        if self.bot is None:
            logger.info("Skipping month-end summary because Telegram bot is disabled.")
            return

        from app.utils.datetime import now_in_timezone

        async with async_session_factory() as session:
            users = await UserRepository(session).list_users()
            today = now_in_timezone(self.settings.default_timezone).date()
            analytics_service = AnalyticsService(session)
            household_repository = HouseholdRepository(session)
            for user in users:
                try:
                    household = await household_repository.ensure_household_for_user(user=user)
                    summary = await analytics_service.grocery_spend_summary(
                        household_id=household.id,
                        today=today,
                        period="this month",
                        through_end_of_period=True,
                    )
                    await self.bot.send_message(
                        chat_id=user.telegram_chat_id,
                        text="Month-end grocery summary\n\n" + summary,
                    )
                except Exception:
                    logger.exception("Failed to send month-end summary to user %s", user.id)

    @staticmethod
    def _parse_time(value: str) -> tuple[int, int]:
        parsed = datetime.strptime(value, "%H:%M")
        return parsed.hour, parsed.minute

    @staticmethod
    def _today(timezone: str) -> date:
        return datetime.now(ZoneInfo(timezone)).date()

    @staticmethod
    def _is_user_time(timezone: str, hhmm: str) -> bool:
        now = datetime.now(ZoneInfo(timezone))
        hour, minute = SchedulerService._parse_time(hhmm)
        return now.hour == hour and now.minute == minute

    @staticmethod
    def _prompt_was_sent(raw_notes: list[object], prompt_key: str) -> bool:
        for note in raw_notes:
            if not isinstance(note, dict):
                continue
            if note.get("text") == prompt_key:
                return True
        return False
