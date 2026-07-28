from calendar import monthrange
from datetime import date, time
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.config import get_settings
from app.db.models import (
    ActivityAction,
    PlanningConversationState,
    Routine,
    ShoppingItem,
    TaskStatus,
)
from app.db.repositories.activity import ActivityRepository
from app.db.repositories.calendar import CalendarRepository
from app.db.repositories.finance import FinanceRepository
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.planning import PlanningRepository
from app.db.repositories.prices import PriceRepository
from app.db.repositories.receipts import ReceiptRepository
from app.db.repositories.routines import RoutineRepository
from app.db.repositories.shopping import ShoppingRepository
from app.db.repositories.tasks import TaskRepository
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory
from app.services.calendar_service import CalendarService, CalendarSyncError
from app.services.dashboard_service import DashboardService
from app.services.finance_category_service import FinanceCategoryService
from app.services.planning_service import PlanningService
from app.services.price_service import PriceService
from app.utils.datetime import now_in_timezone

router = APIRouter()


class ManualPriceRequest(BaseModel):
    shopping_item_id: UUID
    store_name: str
    price: str
    product_name: str | None = None


class RoutineRequest(BaseModel):
    title: str
    duration_minutes: int
    duration_max: int | None = None
    is_active: bool = True


class DayTaskRequest(BaseModel):
  title: str
  due_date: str | date | None = None


class DayTaskMoveRequest(BaseModel):
  due_date: date


class DayEventMoveRequest(BaseModel):
  day: date
  event_ref: str
  target_day: date


class PlanningDefaultsRequest(BaseModel):
  work_start: str | None = None
  work_end: str | None = None
  wake_time: str | None = None
  bed_time: str | None = None
  commute_start: str | None = None
  commute_end: str | None = None
  breakfast_start: str | None = None
  breakfast_end: str | None = None
  lunch_start: str | None = None
  lunch_end: str | None = None
  dinner_start: str | None = None
  dinner_end: str | None = None


class CalendarSettingsRequest(BaseModel):
  google_calendar_id: str


DEFAULTS_PLAN_DATE = date(1900, 1, 1)


def _parse_hhmm_or_none(value: str | None) -> time | None:
  if value is None:
    return None
  text = value.strip()
  if not text:
    return None
  try:
    parts = text.split(":")
    if len(parts) < 2:
      raise ValueError("Expected HH:MM or HH:MM:SS")
    hour = int(parts[0])
    minute = int(parts[1])
    return time(hour=hour, minute=minute)
  except (TypeError, ValueError) as exc:
    raise HTTPException(status_code=400, detail=f"Invalid time value: {value}") from exc


def _parse_dashboard_date(value: str | date | None, *, field_name: str) -> date | None:
  if value is None:
    return None
  if isinstance(value, date):
    return value
  text = value.strip()
  if not text:
    return None
  try:
    if "-" in text and len(text.split("-", 1)[0]) == 4:
      return date.fromisoformat(text)
    for separator in ("/", "-"):
      if separator in text:
        left, middle, right = text.split(separator)
        if len(right) == 4:
          return date(int(right), int(middle), int(left))
  except (TypeError, ValueError):
    pass
  raise HTTPException(status_code=400, detail=f"Invalid {field_name} date. Use YYYY-MM-DD.")


def _build_defaults_notes(payload: PlanningDefaultsRequest) -> str | None:
  notes: list[str] = []
  if payload.wake_time:
    notes.append(f"wake up at {payload.wake_time.strip()}")
  if payload.bed_time:
    notes.append(f"go to sleep at {payload.bed_time.strip()}")
  if payload.commute_start and payload.commute_end:
    notes.append(f"commute from {payload.commute_start.strip()} to {payload.commute_end.strip()}")
  if payload.breakfast_start and payload.breakfast_end:
    notes.append(f"breakfast from {payload.breakfast_start.strip()} to {payload.breakfast_end.strip()}")
  if payload.lunch_start and payload.lunch_end:
    notes.append(f"lunch from {payload.lunch_start.strip()} to {payload.lunch_end.strip()}")
  if payload.dinner_start and payload.dinner_end:
    notes.append(f"dinner from {payload.dinner_start.strip()} to {payload.dinner_end.strip()}")
  return "; ".join(notes) if notes else None


def _clean_calendar_id(value: str) -> str:
  calendar_id = value.strip()
  if not calendar_id:
    raise HTTPException(status_code=400, detail="Calendar ID is required.")
  if len(calendar_id) > 255:
    raise HTTPException(status_code=400, detail="Calendar ID is too long.")
  if any(char.isspace() for char in calendar_id):
    raise HTTPException(status_code=400, detail="Calendar ID cannot contain spaces.")
  return calendar_id


def _extract_defaults_values(*, work_start: time | None, work_end: time | None, notes: str | None) -> dict[str, object]:
  import re

  text = notes or ""

  def find(pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1) if match else None

  return {
    "work_start": work_start.strftime("%H:%M") if work_start else None,
    "work_end": work_end.strftime("%H:%M") if work_end else None,
    "wake_time": find(r"wake up at\s+(\d{1,2}:\d{2})"),
    "bed_time": find(r"go to sleep at\s+(\d{1,2}:\d{2})"),
    "commute_start": find(r"commute from\s+(\d{1,2}:\d{2})"),
    "commute_end": find(r"commute from\s+\d{1,2}:\d{2}\s+to\s+(\d{1,2}:\d{2})"),
    "breakfast_start": find(r"breakfast from\s+(\d{1,2}:\d{2})"),
    "breakfast_end": find(r"breakfast from\s+\d{1,2}:\d{2}\s+to\s+(\d{1,2}:\d{2})"),
    "lunch_start": find(r"lunch from\s+(\d{1,2}:\d{2})"),
    "lunch_end": find(r"lunch from\s+\d{1,2}:\d{2}\s+to\s+(\d{1,2}:\d{2})"),
    "dinner_start": find(r"dinner from\s+(\d{1,2}:\d{2})"),
    "dinner_end": find(r"dinner from\s+\d{1,2}:\d{2}\s+to\s+(\d{1,2}:\d{2})"),
  }


class TransactionCategoryRequest(BaseModel):
    category: str


async def _dashboard_context(request: Request, session):
    email = request.session.get("google_email")
    if not email:
        return None, None
    dashboard_user = await UserRepository(session).get_by_google_email(google_email=str(email))
    if dashboard_user is None or not dashboard_user.family_dashboard_enabled:
        request.session.clear()
        return None, None
    household = await HouseholdRepository(session).ensure_household_for_user(user=dashboard_user)
    return dashboard_user, household


@router.get("/api/dashboard")
async def dashboard_data(
    request: Request,
    month: str | None = None,
    scope: str = "month",
    start_month: str | None = None,
    end_month: str | None = None,
    year: int | None = None,
) -> dict[str, object]:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        today = now_in_timezone(dashboard_user.timezone).date()
        selected_month = None
        period_start = None
        period_end = None
        period_label = None

        def parse_month(value: str) -> date:
            try:
                parsed_year, parsed_month = value.split("-", 1)
                return date(int(parsed_year), int(parsed_month), 1)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Month must be YYYY-MM.") from exc

        if scope == "year":
            selected_year = year or today.year
            period_start = date(selected_year, 1, 1)
            period_end = date(selected_year, 12, 31)
            period_label = str(selected_year)
        elif scope == "range":
            if not start_month or not end_month:
                raise HTTPException(status_code=400, detail="Range requires start_month and end_month.")
            period_start = parse_month(start_month)
            end_start = parse_month(end_month)
            period_end = date(end_start.year, end_start.month, monthrange(end_start.year, end_start.month)[1])
            if period_start > period_end:
                raise HTTPException(status_code=400, detail="Start month must be before end month.")
            period_label = f"{period_start.strftime('%b %Y')} - {end_start.strftime('%b %Y')}"
        elif month:
            selected_month = parse_month(month)
        return await DashboardService(session).summary(
            household_id=household.id,
            today=today,
            selected_month=selected_month,
            period_start=period_start,
            period_end=period_end,
            period_label=period_label,
        )


@router.get("/api/calendar/settings")
async def calendar_settings(request: Request) -> dict[str, object]:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        connections = await CalendarRepository(session).list_google_connections(household_id=household.id)
        configured_id = (
            household.google_calendar_id
            or (connections[0].external_account_id if connections else None)
            or get_settings().google_calendar_id
            or "primary"
        )
        return {
            "google_calendar_id": configured_id,
            "connected": bool(connections),
            "connection_count": len(connections),
        }


@router.put("/api/calendar/settings")
async def update_calendar_settings(
    request: Request,
    payload: CalendarSettingsRequest,
) -> dict[str, object]:
    calendar_id = _clean_calendar_id(payload.google_calendar_id)
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        await HouseholdRepository(session).update_google_calendar_id(
            household=household,
            calendar_id=calendar_id,
        )
        updated_connections = await CalendarRepository(session).update_google_calendar_id_for_household(
            household_id=household.id,
            calendar_id=calendar_id,
        )
        await ActivityRepository(session).log(
            household_id=household.id,
            user_id=dashboard_user.id,
            action=ActivityAction.updated,
            entity_type="calendar_settings",
            entity_id=household.id,
            summary="Updated Google Calendar ID from dashboard.",
        )
        return {
            "saved": True,
            "google_calendar_id": calendar_id,
            "updated_connections": updated_connections,
        }


@router.post("/api/calendar/sync")
async def sync_dashboard_calendars(request: Request) -> dict[str, int]:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        service = CalendarService(session)
        ical_count = await service.sync_ical_feeds(household_id=household.id)
        try:
            google_count = await service.sync_google_connections(household_id=household.id)
        except CalendarSyncError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await ActivityRepository(session).log(
            household_id=household.id,
            user_id=dashboard_user.id,
            action=ActivityAction.updated,
            entity_type="calendar",
            entity_id=household.id,
            summary=f"Synced calendars from dashboard: {google_count} Google event(s), {ical_count} iCal event(s).",
        )
        return {"ical_events": ical_count, "google_events": google_count}


@router.get("/api/activity")
async def activity_data(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=5, le=100),
    search: str | None = None,
    entity_type: str | None = None,
    action: str | None = None,
    category: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, object]:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        return await DashboardService(session).activity_page(
            household_id=household.id,
            page=page,
            page_size=page_size,
            search=search,
            entity_type=entity_type,
            action=action,
            category=category,
            start_date=start_date,
            end_date=end_date,
        )


@router.delete("/api/receipts/{receipt_id}")
async def delete_receipt(request: Request, receipt_id: UUID) -> dict[str, bool]:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        deleted = await ReceiptRepository(session).delete_receipt_for_household(
            receipt_id=receipt_id,
            household_id=household.id,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Receipt not found.")
        await ActivityRepository(session).log(
            household_id=household.id,
            user_id=dashboard_user.id,
            action=ActivityAction.deleted,
            entity_type="receipt",
            entity_id=receipt_id,
            summary="Deleted receipt and extracted items from dashboard.",
        )
        return {"deleted": True}


@router.delete("/api/transactions/{transaction_id}")
async def delete_transaction(request: Request, transaction_id: UUID) -> dict[str, bool]:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        deleted = await FinanceRepository(session).delete_transaction(
            transaction_id=transaction_id,
            household_id=household.id,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        await ActivityRepository(session).log(
            household_id=household.id,
            user_id=dashboard_user.id,
            action=ActivityAction.deleted,
            entity_type="transaction",
            entity_id=transaction_id,
            summary="Deleted bank transaction from dashboard.",
        )
        return {"deleted": True}


@router.delete("/api/shopping/items/{shopping_item_id}")
async def delete_shopping_item(request: Request, shopping_item_id: UUID) -> dict[str, bool]:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        item = await ShoppingRepository(session).remove_pending_item_by_id(
            household_id=household.id,
            item_id=shopping_item_id,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Shopping item not found.")
        await ActivityRepository(session).log(
            household_id=household.id,
            user_id=dashboard_user.id,
            action=ActivityAction.deleted,
            entity_type="shopping_item",
            entity_id=item.id,
            summary=f"Removed shopping item from dashboard: {item.name}",
        )
        return {"deleted": True}


@router.patch("/api/transactions/{transaction_id}/category")
async def update_transaction_category(
    request: Request,
    transaction_id: UUID,
    payload: TransactionCategoryRequest,
) -> dict[str, object]:
    category = payload.category.strip()
    allowed_categories = set(FinanceCategoryService.categories())
    if category not in allowed_categories:
        raise HTTPException(status_code=400, detail="Unsupported category.")
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        transaction = await FinanceRepository(session).update_category(
            transaction_id=transaction_id,
            household_id=household.id,
            category=category,
        )
        if transaction is None:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        await ActivityRepository(session).log(
            household_id=household.id,
            user_id=dashboard_user.id,
            action=ActivityAction.updated,
            entity_type="financial_transaction",
            entity_id=transaction.id,
            summary=f"Changed transaction category: {transaction.description} -> {category}",
            metadata={"category": category},
        )
        return {"updated": True, "category": category}


@router.post("/api/shopping/prices")
async def save_manual_price(request: Request, payload: ManualPriceRequest) -> dict[str, bool]:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")

        shopping_item = await session.get(ShoppingItem, payload.shopping_item_id)
        if shopping_item is None or shopping_item.household_id != household.id:
            raise HTTPException(status_code=404, detail="Shopping item not found.")

        price = payload.price.replace(",", ".").replace("€", "").strip()
        if not price:
            raise HTTPException(status_code=400, detail="Price is required.")
        store_name = payload.store_name.strip()
        if not store_name:
            raise HTTPException(status_code=400, detail="Store is required.")

        await PriceRepository(session).add_quote(
            household_id=household.id,
            shopping_item_id=shopping_item.id,
            item_name=shopping_item.name,
            store_name=store_name,
            product_name=(payload.product_name or shopping_item.name).strip(),
            price=price,
            old_price=None,
            product_url=None,
            is_promotion=False,
            source="dashboard_manual",
            commit=False,
        )
        await ActivityRepository(session).log(
            household_id=household.id,
            user_id=dashboard_user.id,
            action=ActivityAction.created,
            entity_type="shopping_price_quote",
            entity_id=shopping_item.id,
            summary=f"Added manual price: {shopping_item.name} at {store_name} for {price} EUR",
        )
        return {"saved": True}


@router.post("/api/shopping/prices/refresh")
async def refresh_prices(request: Request) -> dict[str, object]:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        count = await PriceService(session).refresh_shopping_prices(household_id=household.id)
        await ActivityRepository(session).log(
            household_id=household.id,
            user_id=dashboard_user.id,
            action=ActivityAction.updated,
            entity_type="shopping_price_quote",
            entity_id=None,
            summary=f"Checked shopping prices: {count} quote(s) saved",
        )
        return {"saved": count}


@router.get("/api/routines")
async def routines_data(request: Request) -> dict[str, object]:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        repository = RoutineRepository(session)
        await repository.ensure_defaults(household_id=household.id)
        routines = await repository.list_for_household(household_id=household.id)
        return {
            "items": [
                {
                    "id": str(routine.id),
                    "title": routine.title,
                    "duration_minutes": int(
                        (routine.schedule or {}).get("duration_minutes")
                        or (routine.schedule or {}).get("duration_min")
                        or 30
                    ),
                    "duration_max": int(
                        (routine.schedule or {}).get("duration_max")
                        or (routine.schedule or {}).get("duration_minutes")
                        or 30
                    ),
                    "is_active": routine.is_active,
                }
                for routine in routines
            ]
        }


@router.post("/api/routines")
async def create_routine(request: Request, payload: RoutineRequest) -> dict[str, object]:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Title is required.")
        if payload.duration_minutes < 1 or payload.duration_minutes > 360:
            raise HTTPException(status_code=400, detail="Duration must be between 1 and 360 minutes.")
        routine = await RoutineRepository(session).create(
            household_id=household.id,
            title=title,
            duration_minutes=payload.duration_minutes,
            duration_max=payload.duration_max,
        )
        await ActivityRepository(session).log(
            household_id=household.id,
            user_id=dashboard_user.id,
            action=ActivityAction.created,
            entity_type="routine",
            entity_id=routine.id,
            summary=f"Added must task: {routine.title}",
        )
        return {"saved": True, "id": str(routine.id)}


@router.patch("/api/routines/{routine_id}")
async def update_routine(request: Request, routine_id: UUID, payload: RoutineRequest) -> dict[str, bool]:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        repository = RoutineRepository(session)
        routine = await session.get(Routine, routine_id)
        if routine is None or routine.household_id != household.id:
            raise HTTPException(status_code=404, detail="Routine not found.")
        if not payload.title.strip():
            raise HTTPException(status_code=400, detail="Title is required.")
        await repository.update(
            routine=routine,
            title=payload.title,
            duration_minutes=payload.duration_minutes,
            duration_max=payload.duration_max,
            is_active=payload.is_active,
        )
        await ActivityRepository(session).log(
            household_id=household.id,
            user_id=dashboard_user.id,
            action=ActivityAction.updated,
            entity_type="routine",
            entity_id=routine.id,
            summary=f"Updated must task: {routine.title}",
        )
        return {"saved": True}


@router.delete("/api/routines/{routine_id}")
async def delete_routine(request: Request, routine_id: UUID) -> dict[str, bool]:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        routine = await session.get(Routine, routine_id)
        if routine is None or routine.household_id != household.id:
            raise HTTPException(status_code=404, detail="Routine not found.")
        title = routine.title
        await RoutineRepository(session).delete(routine=routine)
        await ActivityRepository(session).log(
            household_id=household.id,
            user_id=dashboard_user.id,
            action=ActivityAction.deleted,
            entity_type="routine",
            entity_id=routine_id,
            summary=f"Deleted must task: {title}",
        )
        return {"deleted": True}


@router.get("/api/tasks/day")
async def day_tasks_data(request: Request, day: date | None = None) -> dict[str, object]:
  async with async_session_factory() as session:
    dashboard_user, household = await _dashboard_context(request, session)
    if dashboard_user is None or household is None:
      raise HTTPException(status_code=401, detail="Dashboard login required.")

    selected_day = day or now_in_timezone(dashboard_user.timezone).date()
    task_repository = TaskRepository(session)
    pending_tasks = await task_repository.list_pending_for_user(
      user_id=dashboard_user.id,
      through_date=selected_day,
    )
    day_tasks = [task for task in pending_tasks if task.due_date in {None, selected_day}]

    calendar_events = await CalendarService(session).list_events_for_day(
      household_id=household.id,
      day=selected_day,
      timezone=dashboard_user.timezone,
    )
    conversation = await PlanningRepository(session).get_conversation(
      user_id=dashboard_user.id,
      plan_date=selected_day,
    )
    note_events = []
    note_parts = [part.strip() for part in (conversation.unusual_notes or "").split(";") if part.strip()] if conversation else []
    for idx, part in enumerate(note_parts):
      parsed = PlanningService._fixed_events_from_notes(part)
      for event in parsed:
        note_events.append(
          {
            "title": str(event.get("title") or "Event"),
            "start": str(event.get("start") or ""),
            "end": str(event.get("end") or ""),
            "all_day": bool(event.get("all_day")),
            "source": "note",
            "editable": True,
            "event_ref": f"note:{idx}",
          }
        )

    events = [
      {
        "title": event.title,
        "start": event.starts_at.strftime("%H:%M"),
        "end": event.ends_at.strftime("%H:%M"),
        "all_day": False,
        "source": "calendar",
        "editable": False,
      }
      for event in calendar_events
    ]
    if conversation and conversation.work_start and conversation.work_end:
      events.append(
        {
          "title": "Work",
          "start": conversation.work_start.strftime("%H:%M"),
          "end": conversation.work_end.strftime("%H:%M"),
          "all_day": False,
          "source": "work",
          "editable": False,
        }
      )
    events.extend(note_events)

    return {
      "day": selected_day.isoformat(),
      "items": [
        {
          "id": str(task.id),
          "title": task.title,
          "due_date": task.due_date.isoformat() if task.due_date else None,
          "category": task.category,
          "is_must": (task.category or "").strip().casefold() == "routine",
        }
        for task in day_tasks
      ],
      "events": events,
    }


@router.delete("/api/events/day")
async def delete_day_event(request: Request, day: date, event_ref: str) -> dict[str, bool]:
  async with async_session_factory() as session:
    dashboard_user, household = await _dashboard_context(request, session)
    if dashboard_user is None or household is None:
      raise HTTPException(status_code=401, detail="Dashboard login required.")
    if not event_ref.startswith("note:"):
      raise HTTPException(status_code=400, detail="Only note events can be edited.")
    try:
      index = int(event_ref.split(":", 1)[1])
    except (TypeError, ValueError) as exc:
      raise HTTPException(status_code=400, detail="Invalid event reference.") from exc

    planning_repo = PlanningRepository(session)
    conversation = await planning_repo.get_conversation(user_id=dashboard_user.id, plan_date=day)
    if conversation is None or not conversation.unusual_notes:
      raise HTTPException(status_code=404, detail="Event not found.")
    parts = [part.strip() for part in conversation.unusual_notes.split(";") if part.strip()]
    if index < 0 or index >= len(parts):
      raise HTTPException(status_code=404, detail="Event not found.")

    removed = parts.pop(index)
    await planning_repo.save_answer(
      conversation=conversation,
      message_text=f"Deleted event from dashboard: {removed}",
      unusual_notes="; ".join(parts),
      next_state=PlanningConversationState.complete,
    )
    await ActivityRepository(session).log(
      household_id=household.id,
      user_id=dashboard_user.id,
      action=ActivityAction.deleted,
      entity_type="planning_event",
      entity_id=None,
      summary=f"Deleted event from dashboard: {removed}",
    )
    return {"deleted": True}


@router.patch("/api/events/day/move")
async def move_day_event(request: Request, payload: DayEventMoveRequest) -> dict[str, bool]:
  async with async_session_factory() as session:
    dashboard_user, household = await _dashboard_context(request, session)
    if dashboard_user is None or household is None:
      raise HTTPException(status_code=401, detail="Dashboard login required.")
    if payload.day == payload.target_day:
      raise HTTPException(status_code=400, detail="Source and target day must differ.")
    if not payload.event_ref.startswith("note:"):
      raise HTTPException(status_code=400, detail="Only note events can be edited.")
    try:
      index = int(payload.event_ref.split(":", 1)[1])
    except (TypeError, ValueError) as exc:
      raise HTTPException(status_code=400, detail="Invalid event reference.") from exc

    planning_repo = PlanningRepository(session)
    source = await planning_repo.get_conversation(user_id=dashboard_user.id, plan_date=payload.day)
    if source is None or not source.unusual_notes:
      raise HTTPException(status_code=404, detail="Event not found.")
    source_parts = [part.strip() for part in source.unusual_notes.split(";") if part.strip()]
    if index < 0 or index >= len(source_parts):
      raise HTTPException(status_code=404, detail="Event not found.")

    moved = source_parts.pop(index)
    await planning_repo.save_answer(
      conversation=source,
      message_text=f"Moved event from {payload.day.isoformat()} to {payload.target_day.isoformat()}",
      unusual_notes="; ".join(source_parts),
      next_state=PlanningConversationState.complete,
    )

    target = await planning_repo.get_conversation(user_id=dashboard_user.id, plan_date=payload.target_day)
    if target is None:
      target = await planning_repo.start_conversation(
        user_id=dashboard_user.id,
        household_id=household.id,
        plan_date=payload.target_day,
      )
    target_parts = [part.strip() for part in (target.unusual_notes or "").split(";") if part.strip()]
    if moved.casefold() not in {part.casefold() for part in target_parts}:
      target_parts.append(moved)
    await planning_repo.save_answer(
      conversation=target,
      message_text=f"Moved event in from {payload.day.isoformat()}",
      unusual_notes="; ".join(target_parts),
      next_state=PlanningConversationState.complete,
    )
    await ActivityRepository(session).log(
      household_id=household.id,
      user_id=dashboard_user.id,
      action=ActivityAction.updated,
      entity_type="planning_event",
      entity_id=None,
      summary=f"Moved event to {payload.target_day.isoformat()}: {moved}",
    )
    return {"saved": True}


@router.get("/api/planning-defaults")
async def planning_defaults_data(request: Request) -> dict[str, object]:
  async with async_session_factory() as session:
    dashboard_user, household = await _dashboard_context(request, session)
    if dashboard_user is None or household is None:
      raise HTTPException(status_code=401, detail="Dashboard login required.")
    defaults = await PlanningRepository(session).get_conversation(
      user_id=dashboard_user.id,
      plan_date=DEFAULTS_PLAN_DATE,
    )
    if defaults is None:
      return _extract_defaults_values(work_start=None, work_end=None, notes=None)
    return _extract_defaults_values(
      work_start=defaults.work_start,
      work_end=defaults.work_end,
      notes=defaults.unusual_notes,
    )


@router.put("/api/planning-defaults")
async def save_planning_defaults(request: Request, payload: PlanningDefaultsRequest) -> dict[str, bool]:
  async with async_session_factory() as session:
    dashboard_user, household = await _dashboard_context(request, session)
    if dashboard_user is None or household is None:
      raise HTTPException(status_code=401, detail="Dashboard login required.")
    planning_repo = PlanningRepository(session)
    defaults = await planning_repo.get_conversation(
      user_id=dashboard_user.id,
      plan_date=DEFAULTS_PLAN_DATE,
    )
    if defaults is None:
      defaults = await planning_repo.start_conversation(
        user_id=dashboard_user.id,
        household_id=household.id,
        plan_date=DEFAULTS_PLAN_DATE,
      )
    notes = _build_defaults_notes(payload)
    await planning_repo.save_answer(
      conversation=defaults,
      message_text="Updated planning defaults from dashboard",
      work_start=_parse_hhmm_or_none(payload.work_start),
      work_end=_parse_hhmm_or_none(payload.work_end),
      unusual_notes=notes,
      next_state=PlanningConversationState.complete,
    )
    await ActivityRepository(session).log(
      household_id=household.id,
      user_id=dashboard_user.id,
      action=ActivityAction.updated,
      entity_type="planning_defaults",
      entity_id=None,
      summary="Updated planning defaults from dashboard",
    )
    return {"saved": True}


@router.post("/api/tasks")
async def create_day_task(request: Request, payload: DayTaskRequest) -> dict[str, object]:
  async with async_session_factory() as session:
    dashboard_user, household = await _dashboard_context(request, session)
    if dashboard_user is None or household is None:
      raise HTTPException(status_code=401, detail="Dashboard login required.")
    title = payload.title.strip()
    if not title:
      raise HTTPException(status_code=400, detail="Title is required.")
    due = _parse_dashboard_date(payload.due_date, field_name="due") or now_in_timezone(dashboard_user.timezone).date()

    parsed_events = PlanningService._fixed_events_from_notes(title)
    if parsed_events:
      planning_repo = PlanningRepository(session)
      conversation = await planning_repo.get_conversation(
        user_id=dashboard_user.id,
        plan_date=due,
      )
      if conversation is None:
        conversation = await planning_repo.start_conversation(
          user_id=dashboard_user.id,
          household_id=household.id,
          plan_date=due,
        )
      note_parts = [part.strip() for part in (conversation.unusual_notes or "").split(";") if part.strip()]
      if title.casefold() not in {part.casefold() for part in note_parts}:
        note_parts.append(title)
      await planning_repo.save_answer(
        conversation=conversation,
        message_text=f"Added event from dashboard: {title}",
        unusual_notes="; ".join(note_parts),
        next_state=PlanningConversationState.complete,
      )
      await ActivityRepository(session).log(
        household_id=household.id,
        user_id=dashboard_user.id,
        action=ActivityAction.created,
        entity_type="planning_event",
        entity_id=None,
        summary=f"Added day event from dashboard: {title}",
      )
      return {"saved": True, "kind": "event"}

    task = await TaskRepository(session).create_task(
      user_id=dashboard_user.id,
      household_id=household.id,
      title=title,
      due_date=due,
    )
    await ActivityRepository(session).log(
      household_id=household.id,
      user_id=dashboard_user.id,
      action=ActivityAction.created,
      entity_type="task",
      entity_id=task.id,
      summary=f"Added day task from dashboard: {task.title}",
    )
    return {"saved": True, "id": str(task.id), "kind": "task"}


@router.patch("/api/tasks/{task_id}/move")
async def move_day_task(request: Request, task_id: UUID, payload: DayTaskMoveRequest) -> dict[str, bool]:
  async with async_session_factory() as session:
    dashboard_user, household = await _dashboard_context(request, session)
    if dashboard_user is None or household is None:
      raise HTTPException(status_code=401, detail="Dashboard login required.")
    repository = TaskRepository(session)
    task = await repository.get_user_task(task_id=task_id, user_id=dashboard_user.id)
    if task is None or task.household_id != household.id:
      raise HTTPException(status_code=404, detail="Task not found.")
    if (task.category or "").strip().casefold() == "routine":
      raise HTTPException(status_code=400, detail="Must tasks repeat daily and cannot be moved.")
    task.due_date = payload.due_date
    task.moved_count += 1
    await session.commit()
    await session.refresh(task)
    await ActivityRepository(session).log(
      household_id=household.id,
      user_id=dashboard_user.id,
      action=ActivityAction.updated,
      entity_type="task",
      entity_id=task.id,
      summary=f"Rescheduled task from dashboard: {task.title} -> {task.due_date.isoformat()}",
    )
    return {"saved": True}


@router.delete("/api/tasks/{task_id}")
async def delete_day_task(request: Request, task_id: UUID) -> dict[str, bool]:
  async with async_session_factory() as session:
    dashboard_user, household = await _dashboard_context(request, session)
    if dashboard_user is None or household is None:
      raise HTTPException(status_code=401, detail="Dashboard login required.")
    repository = TaskRepository(session)
    task = await repository.get_user_task(task_id=task_id, user_id=dashboard_user.id)
    if task is None or task.household_id != household.id:
      raise HTTPException(status_code=404, detail="Task not found.")
    task.status = TaskStatus.skipped
    await session.commit()
    await session.refresh(task)
    await ActivityRepository(session).log(
      household_id=household.id,
      user_id=dashboard_user.id,
      action=ActivityAction.deleted,
      entity_type="task",
      entity_id=task.id,
      summary=f"Removed day task from dashboard: {task.title}",
    )
    return {"deleted": True}


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> str:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            return _login_page()
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Family Copilot Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f6f8;
      --surface: #ffffff;
      --surface-soft: #f9fafb;
      --ink: #17181c;
      --muted: #70737c;
      --faint: #9ca0aa;
      --line: #e5e7eb;
      --accent: #2563eb;
      --accent-soft: #dbeafe;
      --green: #15803d;
      --danger: #dc2626;
      --danger-soft: #fee2e2;
      --shadow: 0 1px 2px rgba(15, 23, 42, .05), 0 16px 40px rgba(15, 23, 42, .06);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      letter-spacing: 0;
    }
    button, input, select { font: inherit; }
    .shell {
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
      padding: 28px 0 48px;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 22px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
    }
    h1 {
      margin: 0;
      font-size: 28px;
      line-height: 1.12;
      font-weight: 650;
      letter-spacing: 0;
    }
    h2 {
      margin: 0;
      font-size: 15px;
      line-height: 1.3;
      font-weight: 650;
      letter-spacing: 0;
    }
    .subtitle, .muted { color: var(--muted); }
    .subtitle { font-size: 14px; }
    .status {
      color: var(--muted);
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 11px;
      white-space: nowrap;
      box-shadow: 0 1px 2px rgba(15, 23, 42, .04);
    }
    .top-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .month-control {
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--surface);
      color: var(--ink);
      padding: 7px 11px;
      box-shadow: 0 1px 2px rgba(15, 23, 42, .04);
    }
    .period-range { display: none; gap: 8px; align-items: center; }
    .period-range.open { display: flex; }
    .activity-pill {
      display: inline-block;
      border-radius: 999px;
      padding: 2px 7px;
      font-size: 11px;
      font-weight: 650;
      background: #eef2ff;
      color: #2563eb;
    }
    .activity-pill.shopping { background: #dcfce7; color: #15803d; }
    .activity-pill.task, .activity-pill.routine { background: #dbeafe; color: #2563eb; }
    .activity-pill.finance, .activity-pill.receipt { background: #fef3c7; color: #a16207; }
    .grid { display: grid; gap: 14px; }
    .hidden { display: none !important; }
    .tabs { display: flex; gap: 8px; }
    .tab-btn {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 999px;
      padding: 8px 12px;
      color: var(--muted);
      cursor: pointer;
    }
    .tab-btn.active { color: var(--ink); border-color: #cbd5e1; box-shadow: 0 1px 2px rgba(15,23,42,.05); }
    .view { margin-top: 14px; }
    .view.hidden { display: none; }
    .metrics { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .main-grid {
      grid-template-columns: minmax(0, 1.15fr) minmax(310px, .85fr);
      align-items: start;
    }
    .panel, .metric {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: var(--shadow);
    }
    .panel { padding: 18px; }
    .metric { padding: 16px; min-height: 112px; }
    .metric-clickable:hover { background: var(--line); }
    .metric-clickable:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    .income-amount { color: #1a8a4a; font-weight: 650; }
    .metric-label {
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 10px;
    }
    .metric-value {
      font-size: 25px;
      line-height: 1.1;
      font-weight: 680;
      letter-spacing: 0;
    }
    .metric-note {
      color: var(--muted);
      font-size: 13px;
      margin-top: 10px;
    }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }
    .section-foot {
      margin-top: 10px;
      text-align: center;
    }
    .section-foot .link-btn[hidden] { display: none; }
    .link-btn {
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--surface);
      color: var(--muted);
      cursor: pointer;
      padding: 6px 10px;
      font-size: 12px;
    }
    .icon-btn {
      width: 34px;
      height: 34px;
      display: inline-grid;
      place-items: center;
      padding: 0;
    }
    .icon-btn svg {
      width: 17px;
      height: 17px;
      stroke: currentColor;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
      fill: none;
    }
    .link-btn:hover { color: var(--ink); border-color: #cbd5e1; }
    a.link-btn { text-decoration: none; display: inline-flex; align-items: center; }
    .rows { display: grid; gap: 6px; }
    .filters {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 14px;
    }
    .filters input, .filters select {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px 9px;
      background: #fff;
      color: var(--ink);
    }
    .routine-form, .routine-row {
      display: grid;
      grid-template-columns: minmax(180px, 1fr) 110px 110px auto auto;
      gap: 8px;
      align-items: center;
    }
    #task-metrics {
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }
    #task-metrics .metric {
      min-height: 80px;
      padding: 12px 14px;
    }
    #task-metrics .metric-label {
      margin-bottom: 6px;
      font-size: 12px;
    }
    #task-metrics .metric-value {
      font-size: 36px;
      line-height: 1;
      font-weight: 700;
    }
    .routine-form {
      margin: 10px 0 14px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--surface-soft);
      margin-bottom: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--line);
    }
    .routine-form input, .routine-row input {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px 9px;
      background: #fff;
      color: var(--ink);
    }
    .routine-row {
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      margin-bottom: 8px;
      border-bottom: 1px solid var(--line);
    }
    .routine-row:last-child { border-bottom: 1px solid var(--line); }
    .day-head {
      margin-top: 22px;
      padding-top: 16px;
      border-top: 1px solid var(--line);
    }
    .day-picker-wrap {
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    .day-picker-wrap input {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 7px 9px;
      background: #fff;
      color: var(--ink);
    }
    .day-task-form {
      display: grid;
      grid-template-columns: minmax(180px, 1fr) auto;
      gap: 8px;
      margin-bottom: 14px;
    }
    .defaults-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 12px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--surface-soft);
    }
    .defaults-grid label {
      display: grid;
      gap: 4px;
    }
    .defaults-grid input {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 7px 8px;
      background: #fff;
      color: var(--ink);
    }
    .day-task-form input {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px 9px;
      background: #fff;
      color: var(--ink);
    }
    .day-layout {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .day-card {
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--surface-soft);
      padding: 12px;
    }
    .section-head.compact { margin-bottom: 8px; }
    .day-task-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
      padding: 9px 0;
      border-bottom: 1px solid var(--line);
    }
    .day-task-row:last-child { border-bottom: 0; }
    .day-task-actions {
      display: inline-flex;
      gap: 6px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .mini-btn {
      border: 1px solid #cbd5e1;
      border-radius: 999px;
      background: #fff;
      color: var(--ink);
      padding: 4px 9px;
      font-size: 12px;
      cursor: pointer;
      white-space: nowrap;
    }
    .event-row {
      padding: 8px 0;
      border-bottom: 1px solid var(--line);
    }
    .event-row:last-child { border-bottom: 0; }
    .event-actions {
      margin-top: 6px;
      display: inline-flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .check-label {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      white-space: nowrap;
    }
    .primary-btn {
      border: 1px solid #cbd5e1;
      border-radius: 10px;
      background: #fff;
      padding: 8px 11px;
      cursor: pointer;
      font-weight: 600;
    }
    .row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      min-height: 42px;
      padding: 9px 0;
      border-bottom: 1px solid var(--line);
    }
    .row:last-child { border-bottom: 0; }
    .row-main { min-width: 0; }
    .row-title {
      overflow-wrap: anywhere;
      line-height: 1.35;
    }
    .row-sub {
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }
    .amount { font-weight: 650; white-space: nowrap; }
    .transaction-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-shrink: 0;
      justify-content: flex-end;
    }
    .category-select {
      width: 100px;
      min-width: 0;
      max-width: 100px;
      border: 1px solid #d8dde6;
      background: #fff;
      color: var(--ink);
      border-radius: 999px;
      padding: 7px 22px 7px 11px;
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .bar-row {
      display: grid;
      grid-template-columns: minmax(90px, 150px) minmax(120px, 1fr) 88px;
      gap: 12px;
      align-items: center;
      min-height: 38px;
      width: 100%;
      border: 0;
      border-radius: 10px;
      background: transparent;
      color: inherit;
      text-align: left;
      cursor: pointer;
      padding: 4px;
    }
    .bar-row:hover { background: var(--surface-soft); }
    .bar-track {
      height: 7px;
      background: #eef1f5;
      border-radius: 999px;
      overflow: hidden;
    }
    .bar { height: 100%; background: var(--accent); border-radius: inherit; }
    .category {
      padding-top: 2px;
      margin-top: 14px;
    }
    .category:first-child { margin-top: 0; }
    .category-title {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      margin-bottom: 4px;
      text-transform: uppercase;
    }
    .price-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }
    .promo {
      color: var(--green);
      font-weight: 600;
    }
    .old-price {
      color: var(--faint);
      text-decoration: line-through;
    }
    .hidden-row { display: none; }
    .price-form {
      display: none;
      grid-template-columns: minmax(90px, 1fr) minmax(80px, .7fr) auto;
      gap: 8px;
      margin-top: 8px;
    }
    .price-form.open { display: grid; }
    .price-form input {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 7px 8px;
      background: #fff;
    }
    .price-form button {
      border: 1px solid #cbd5e1;
      border-radius: 10px;
      background: #fff;
      padding: 7px 10px;
      cursor: pointer;
      font-weight: 600;
    }
    .settings-form {
      display: grid;
      gap: 12px;
    }
    .settings-form label {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }
    .settings-form input {
      width: 100%;
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px 11px;
      background: #fff;
      color: var(--ink);
      font-size: 13px;
    }
    .settings-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .calendar-agenda {
      display: grid;
      gap: 12px;
      margin-bottom: 18px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--line);
    }
    .calendar-day-control {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .calendar-day-control label {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }
    .calendar-day-control input {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 9px 10px;
      background: #fff;
      color: var(--ink);
      font-size: 13px;
    }
    .calendar-settings-title {
      margin: 0;
      font-size: 13px;
      font-weight: 700;
    }
    .calendar-embed {
      display: grid;
      gap: 8px;
    }
    .calendar-embed iframe {
      width: 100%;
      min-height: 420px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: #fff;
    }
    .calendar-embed-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .recommendation {
      border: 1px solid var(--line);
      background: var(--surface-soft);
      border-radius: 14px;
      padding: 12px;
      line-height: 1.45;
    }
    .chart {
      width: 100%;
      height: auto;
      display: block;
      overflow: visible;
    }
    .chart-card {
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--surface-soft);
      padding: 8px;
    }
    .chart-legend {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
    }
    .legend-dot {
      display: inline-block;
      width: 8px;
      height: 8px;
      border-radius: 999px;
      margin-right: 6px;
    }
    .delete-btn {
      flex: 0 0 auto;
      width: 32px;
      height: 32px;
      display: inline-grid;
      place-items: center;
      border: 1px solid transparent;
      border-radius: 999px;
      color: var(--faint);
      background: transparent;
      cursor: pointer;
      transition: background .16s ease, color .16s ease, border-color .16s ease;
    }
    .delete-btn:hover {
      color: var(--danger);
      background: var(--danger-soft);
      border-color: #fecaca;
    }
    .empty {
      color: var(--muted);
      background: var(--surface-soft);
      border: 1px dashed var(--line);
      border-radius: 14px;
      padding: 16px;
    }
    .toast {
      position: fixed;
      right: 18px;
      bottom: 18px;
      max-width: min(360px, calc(100% - 36px));
      padding: 12px 14px;
      border-radius: 14px;
      color: var(--ink);
      background: var(--surface);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      opacity: 0;
      transform: translateY(8px);
      pointer-events: none;
      transition: opacity .18s ease, transform .18s ease;
    }
    .toast.show { opacity: 1; transform: translateY(0); }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      z-index: 40;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 18px;
      background: rgba(15, 23, 42, .22);
      backdrop-filter: blur(8px);
    }
    .modal-backdrop.open { display: flex; }
    .modal {
      width: min(860px, 100%);
      max-height: min(820px, calc(100vh - 36px));
      overflow: auto;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 22px;
      box-shadow: 0 24px 70px rgba(15, 23, 42, .22);
      padding: 28px;
    }
    .modal-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 20px;
    }
    .modal-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 20px;
      margin-bottom: 20px;
    }
    .mini-panel {
      border: 1px solid var(--line);
      border-radius: 16px;
      background: var(--surface-soft);
      padding: 20px;
    }
    .mini-panel h2 { margin-bottom: 16px; }
    .pie-chart {
      width: min(280px, 100%);
      height: auto;
      display: block;
      margin: 0 auto 10px;
    }
    .detail-bars { display: grid; gap: 12px; }
    .detail-bar-row {
      display: grid;
      grid-template-columns: minmax(90px, 130px) minmax(100px, 1fr) 84px;
      gap: 10px;
      align-items: center;
      font-size: 13px;
    }
    @media (max-width: 900px) {
      .metrics, .main-grid { grid-template-columns: 1fr; }
      .topbar { align-items: start; flex-direction: column; }
      .top-actions { justify-content: flex-start; }
      .brand { align-items: start; flex-direction: column; gap: 12px; }
      .status { white-space: normal; }
      .modal-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 620px) {
      .shell { width: min(100% - 22px, 1180px); padding-top: 18px; }
      h1 { font-size: 23px; }
      .panel, .metric { border-radius: 16px; }
      .bar-row { grid-template-columns: 1fr; gap: 7px; padding: 8px 0; }
      .filters { grid-template-columns: 1fr; }
      .routine-form, .routine-row { grid-template-columns: 1fr; }
      #task-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      #task-metrics .metric-value { font-size: 28px; }
      .day-task-form { grid-template-columns: 1fr; }
      .day-layout { grid-template-columns: 1fr; }
      .defaults-grid { grid-template-columns: 1fr; }
      .day-task-row { grid-template-columns: 1fr; }
      .day-task-actions { justify-content: flex-start; }
      .amount { justify-self: start; }
      .row { align-items: start; }
      .price-form { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div class="brand">
        <h1>Family Copilot</h1>
        <nav class="tabs" aria-label="Dashboard views">
          <button class="tab-btn active" type="button" data-view-target="overview-view">Overview</button>
          <button class="tab-btn" type="button" data-view-target="tasks-view">Tasks</button>
          <button class="tab-btn" type="button" data-view-target="activity-view">Activity Log</button>
        </nav>
      </div>
      <div class="top-actions">
        <a class="link-btn" href="/calendar/google/start">Connect calendar</a>
        <button class="link-btn" type="button" id="calendar-settings-open">Calendar</button>
        <select class="month-control" id="scope-filter" aria-label="Dashboard period type">
          <option value="month">Month</option>
          <option value="range">Range</option>
          <option value="year">Year</option>
        </select>
        <input class="month-control" id="month-filter" type="month" aria-label="Dashboard month">
        <div class="period-range" id="range-controls">
          <input class="month-control" id="start-month-filter" type="month" aria-label="Start month">
          <input class="month-control" id="end-month-filter" type="month" aria-label="End month">
        </div>
        <input class="month-control hidden" id="year-filter" type="number" min="2000" max="2100" aria-label="Dashboard year">
        <div class="status" id="status">Loading dashboard</div>
      </div>
    </header>

    <section class="grid metrics" id="metrics"></section>

    <section class="grid main-grid view" id="overview-view">
      <div class="grid">
        <section class="panel">
          <div class="section-head">
            <h2>Spend By Category This Month</h2>
          </div>
          <div id="expense-categories"></div>
        </section>

        <section class="panel">
          <div class="section-head">
            <h2>Shopping List</h2>
            <button class="link-btn" type="button" id="refresh-prices">Check prices</button>
          </div>
          <div id="categories"></div>
          <div class="section-foot"><button class="link-btn" type="button" data-collapse-target="categories">Show all</button></div>
        </section>

        <section class="panel">
          <div class="section-head">
            <h2>Promotions On Things You Buy</h2>
          </div>
          <div class="rows" id="promotions"></div>
          <div class="section-foot"><button class="link-btn" type="button" data-collapse-target="promotions">Show all</button></div>
        </section>

        <section class="panel">
          <div class="section-head">
            <h2>Recommendations</h2>
          </div>
          <div class="rows" id="recommendations"></div>
        </section>
      </div>

      <div class="grid">
        <section class="panel">
          <div class="section-head">
            <h2>Income vs Expenses</h2>
          </div>
          <div class="chart-card" id="cashflow-bars"></div>
        </section>

        <section class="panel">
          <div class="section-head">
            <h2>Monthly Cashflow</h2>
          </div>
          <div class="chart-card" id="cashflow-line"></div>
        </section>

        <section class="panel">
          <div class="section-head">
            <h2>Recent Receipts</h2>
            <span class="muted" id="receipt-count"></span>
          </div>
          <div class="rows" id="receipts"></div>
        </section>

        <section class="panel">
          <div class="section-head">
            <h2>Expense Transactions This Month</h2>
          </div>
          <div class="rows" id="transactions"></div>
          <div class="section-foot"><button class="link-btn" type="button" data-collapse-target="transactions">Show all</button></div>
        </section>
      </div>
    </section>

    <section class="panel view hidden" id="tasks-view">
      <div class="section-head">
        <h2>Must Tasks</h2>
      </div>
      <section class="grid metrics" id="task-metrics"></section>
      <form class="routine-form" id="routine-create">
        <input name="title" placeholder="Task name" autocomplete="off">
        <input name="duration_minutes" type="number" min="1" max="360" placeholder="Min min">
        <input name="duration_max" type="number" min="1" max="360" placeholder="Max min">
        <label class="check-label"><input name="is_active" type="checkbox" checked> Active</label>
        <button class="primary-btn" type="submit">Add</button>
      </form>
      <div class="rows" id="routines"></div>

      <div class="section-head day-head">
        <h2>Defaults</h2>
      </div>
      <form class="defaults-grid" id="defaults-form">
        <label><span class="muted">Work start</span><input name="work_start" type="time"></label>
        <label><span class="muted">Work end</span><input name="work_end" type="time"></label>
        <label><span class="muted">Wake up</span><input name="wake_time" type="time"></label>
        <label><span class="muted">Bedtime</span><input name="bed_time" type="time"></label>
        <label><span class="muted">Commute start</span><input name="commute_start" type="time"></label>
        <label><span class="muted">Commute end</span><input name="commute_end" type="time"></label>
        <label><span class="muted">Breakfast start</span><input name="breakfast_start" type="time"></label>
        <label><span class="muted">Breakfast end</span><input name="breakfast_end" type="time"></label>
        <label><span class="muted">Lunch start</span><input name="lunch_start" type="time"></label>
        <label><span class="muted">Lunch end</span><input name="lunch_end" type="time"></label>
        <label><span class="muted">Dinner start</span><input name="dinner_start" type="time"></label>
        <label><span class="muted">Dinner end</span><input name="dinner_end" type="time"></label>
        <div><button class="primary-btn" type="submit">Save Defaults</button></div>
      </form>

      <div class="section-head day-head">
        <h2>Day Tasks & Events</h2>
        <div class="day-picker-wrap">
          <label for="day-task-date" class="muted">Day</label>
          <input id="day-task-date" type="date" aria-label="Task and event day">
        </div>
      </div>

      <form class="day-task-form" id="day-task-create">
        <input name="title" placeholder="Add task or event for selected day" autocomplete="off">
        <button class="primary-btn" type="submit">Add</button>
      </form>

      <div class="day-layout">
        <section class="day-card">
          <div class="section-head compact">
            <h2>Tasks</h2>
          </div>
          <div class="rows" id="day-tasks"></div>
        </section>
        <section class="day-card">
          <div class="section-head compact">
            <h2>Events</h2>
          </div>
          <div class="rows" id="day-events"></div>
        </section>
      </div>
    </section>

    <section class="panel view hidden" id="activity-view">
      <div class="section-head">
        <h2>Activity Log</h2>
      </div>
      <form class="filters" id="activity-filters">
        <input name="search" placeholder="Search">
        <select name="entity_type"><option value="">All types</option></select>
        <select name="action"><option value="">All actions</option></select>
        <select name="category"><option value="">All categories</option></select>
        <input name="start_date" type="date" aria-label="Start date">
        <input name="end_date" type="date" aria-label="End date">
      </form>
      <div class="rows" id="activity"></div>
      <div class="section-foot">
        <button class="link-btn" type="button" id="activity-prev">Previous</button>
        <span class="muted" id="activity-page"></span>
        <button class="link-btn" type="button" id="activity-next">Next</button>
      </div>
    </section>
  </main>
  <div class="modal-backdrop" id="category-modal" aria-hidden="true">
    <section class="modal" role="dialog" aria-modal="true" aria-labelledby="category-modal-title">
      <div class="modal-head">
        <div>
          <h2 id="category-modal-title">Category</h2>
          <div class="row-sub" id="category-modal-subtitle"></div>
        </div>
        <button class="delete-btn" type="button" id="category-modal-close" aria-label="Close category details" title="Close">
          <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
            <path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
          </svg>
        </button>
      </div>
      <div id="category-modal-body"></div>
    </section>
  </div>
  <div class="modal-backdrop" id="income-modal" aria-hidden="true">
    <section class="modal" role="dialog" aria-modal="true" aria-labelledby="income-modal-title">
      <div class="modal-head">
        <div>
          <h2 id="income-modal-title">Income</h2>
          <div class="row-sub" id="income-modal-subtitle"></div>
        </div>
        <button class="delete-btn" type="button" id="income-modal-close" aria-label="Close income breakdown" title="Close">
          <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
            <path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
          </svg>
        </button>
      </div>
      <div id="income-modal-body"></div>
    </section>
  </div>
  <div class="modal-backdrop" id="calendar-modal" aria-hidden="true">
    <section class="modal" role="dialog" aria-modal="true" aria-labelledby="calendar-modal-title">
      <div class="modal-head">
        <div>
          <h2 id="calendar-modal-title">Calendar</h2>
          <div class="row-sub" id="calendar-modal-subtitle">Loading calendar</div>
        </div>
        <button class="delete-btn" type="button" id="calendar-modal-close" aria-label="Close calendar" title="Close">
          <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
            <path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
          </svg>
        </button>
      </div>
      <section class="calendar-agenda">
        <div class="calendar-day-control">
          <label>
            <span>Synced events for day</span>
            <input id="calendar-day-date" type="date" aria-label="Calendar day">
          </label>
          <button class="link-btn" type="button" id="calendar-refresh-day">Refresh day</button>
        </div>
        <div class="rows" id="calendar-day-events"></div>
        <div class="calendar-embed">
          <div class="calendar-embed-head">
            <h3 class="calendar-settings-title">Google calendar view</h3>
            <span class="muted" id="calendar-embed-status"></span>
          </div>
          <iframe id="google-calendar-embed" title="Google Calendar" loading="lazy"></iframe>
        </div>
      </section>
      <h3 class="calendar-settings-title">Google settings</h3>
      <form class="settings-form" id="calendar-settings-form">
        <label>
          <span>Google Calendar ID</span>
          <input id="google-calendar-id" name="google_calendar_id" autocomplete="off" placeholder="primary or calendar-id@group.calendar.google.com">
        </label>
        <div class="settings-actions">
          <button class="primary-btn" type="submit">Save</button>
          <a class="link-btn" href="/calendar/google/start" id="calendar-connect-link">Connect Google</a>
          <button class="link-btn" type="button" id="calendar-sync-now">Sync now</button>
          <span class="muted" id="calendar-settings-status"></span>
        </div>
      </form>
    </section>
  </div>
  <div class="toast" id="toast"></div>

  <script>
    const moneyNumber = value => Number(String(value || "0").replace(" EUR", "").replace(",", "."));
    const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, char => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }[char]));
    const metric = (label, value, note = "") => `
      <article class="metric">
        <div class="metric-label">${escapeHtml(label)}</div>
        <div class="metric-value">${escapeHtml(value)}</div>
        ${note ? `<div class="metric-note">${escapeHtml(note)}</div>` : ""}
      </article>
    `;
    const clickableMetric = (label, value, action, note = "") => `
      <article class="metric metric-clickable" data-metric-action="${escapeHtml(action)}" role="button" tabindex="0" style="cursor:pointer">
        <div class="metric-label">${escapeHtml(label)}</div>
        <div class="metric-value">${escapeHtml(value)}</div>
        ${note ? `<div class="metric-note">${escapeHtml(note)}</div>` : ""}
      </article>
    `;
    const flattenShopping = groups => groups.flatMap(group => group.items.map(item => ({
      ...item,
      category: group.category,
    })));
    const iconSvg = name => {
      if (name === "edit") {
        return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5Z"/></svg>`;
      }
      if (name === "delete") {
        return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M7 10v9"/><path d="M12 10v9"/><path d="M17 10v9"/></svg>`;
      }
      return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14"/><path d="M5 12h14"/></svg>`;
    };
    const collapseButton = target => document.querySelector(`[data-collapse-target="${target}"]`);
    const setCollapseButton = (target, total, expanded) => {
      const button = collapseButton(target);
      if (!button) return;
      button.hidden = total <= 6;
      button.dataset.expanded = expanded ? "true" : "false";
      button.textContent = expanded ? "Show less" : `Show all ${total}`;
    };
    const chartScale = (value, max, height) => max <= 0 ? height : height - (value / max * height);
    const renderCurrentCashflow = totals => {
      const income = moneyNumber(totals.income_month);
      const expenses = moneyNumber(totals.this_month);
      const max = Math.max(income, expenses, 1);
      const barMaxHeight = 104;
      const incomeHeight = Math.max(3, income / max * barMaxHeight);
      const expenseHeight = Math.max(3, expenses / max * barMaxHeight);
      return `
        <svg class="chart" viewBox="0 0 360 180" role="img" aria-label="Current month income and expenses">
          <line x1="34" y1="132" x2="326" y2="132" stroke="#e5e7eb" stroke-width="1"/>
          <rect x="96" y="${132 - incomeHeight}" width="54" height="${incomeHeight}" rx="10" fill="#2563eb"/>
          <rect x="210" y="${132 - expenseHeight}" width="54" height="${expenseHeight}" rx="10" fill="#ef4444"/>
          <text x="123" y="153" text-anchor="middle" font-size="12" fill="#70737c">Income</text>
          <text x="237" y="153" text-anchor="middle" font-size="12" fill="#70737c">Expenses</text>
          <text x="123" y="${Math.max(18, 124 - incomeHeight)}" text-anchor="middle" font-size="12" font-weight="650" fill="#17181c">${escapeHtml(totals.income_month)}</text>
          <text x="237" y="${Math.max(18, 124 - expenseHeight)}" text-anchor="middle" font-size="12" font-weight="650" fill="#17181c">${escapeHtml(totals.this_month)}</text>
        </svg>
        <div class="chart-legend">
          <span><span class="legend-dot" style="background:#2563eb"></span>Income</span>
          <span><span class="legend-dot" style="background:#ef4444"></span>Expenses</span>
        </div>
      `;
    };
    const renderMonthlyCashflow = series => {
      const rows = series && series.length ? series : [];
      if (!rows.length) return `<div class="empty">No monthly data yet.</div>`;

      const width = 540;
      const height = 230;
      const left = 44;
      const right = 18;
      const top = 20;
      const bottom = 42;
      const chartWidth = width - left - right;
      const chartHeight = height - top - bottom;
      const max = Math.max(...rows.flatMap(row => [Number(row.income_value || 0), Number(row.expenses_value || 0)]), 1);
      const xFor = index => left + (rows.length === 1 ? chartWidth / 2 : index * (chartWidth / (rows.length - 1)));
      const yFor = value => top + chartScale(Number(value || 0), max, chartHeight);
      const incomePoints = rows.map((row, index) => `${xFor(index)},${yFor(row.income_value)}`).join(" ");
      const expensePoints = rows.map((row, index) => `${xFor(index)},${yFor(row.expenses_value)}`).join(" ");
      const labels = rows.map((row, index) => `
        <text x="${xFor(index)}" y="${height - 16}" text-anchor="middle" font-size="11" fill="#70737c">${escapeHtml(row.label)}</text>
      `).join("");
      return `
        <svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Income and expenses over months">
          <line x1="${left}" y1="${top + chartHeight}" x2="${width - right}" y2="${top + chartHeight}" stroke="#e5e7eb"/>
          <line x1="${left}" y1="${top}" x2="${left}" y2="${top + chartHeight}" stroke="#e5e7eb"/>
          <text x="${left - 10}" y="${top + 4}" text-anchor="end" font-size="11" fill="#9ca0aa">${max.toFixed(0)}</text>
          <text x="${left - 10}" y="${top + chartHeight}" text-anchor="end" font-size="11" fill="#9ca0aa">0</text>
          <polyline points="${expensePoints}" fill="none" stroke="#ef4444" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
          <polyline points="${incomePoints}" fill="none" stroke="#2563eb" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
          ${rows.map((row, index) => `
            <circle cx="${xFor(index)}" cy="${yFor(row.expenses_value)}" r="4" fill="#ef4444"/>
            <circle cx="${xFor(index)}" cy="${yFor(row.income_value)}" r="4" fill="#2563eb"/>
          `).join("")}
          ${labels}
        </svg>
        <div class="chart-legend">
          <span><span class="legend-dot" style="background:#2563eb"></span>Income</span>
          <span><span class="legend-dot" style="background:#ef4444"></span>Expenses</span>
        </div>
      `;
    };
    let activityPage = 1;
    let latestDashboardData = null;
    const currentMonthValue = () => {
      const now = new Date();
      return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
    };
    const dashboardQuery = () => {
      const scope = document.querySelector("#scope-filter").value;
      const params = new URLSearchParams({ scope });
      if (scope === "year") {
        params.set("year", document.querySelector("#year-filter").value || String(new Date().getFullYear()));
      } else if (scope === "range") {
        params.set("start_month", document.querySelector("#start-month-filter").value || currentMonthValue());
        params.set("end_month", document.querySelector("#end-month-filter").value || currentMonthValue());
      } else {
        params.set("month", document.querySelector("#month-filter").value || currentMonthValue());
      }
      return params.toString();
    };
    const renderBars = (rows, emptyText) => {
      const max = Math.max(...rows.map(x => moneyNumber(x.value)), 1);
      return rows.length ? rows.map(row => {
        const width = Math.max(4, moneyNumber(row.value) / max * 100);
        return `
          <button class="bar-row" type="button" data-category-detail="${escapeHtml(row.label)}">
            <div class="row-title">${escapeHtml(row.label)}</div>
            <div class="bar-track"><div class="bar" style="width:${width}%"></div></div>
            <div class="amount">${escapeHtml(row.value)}</div>
          </button>
        `;
      }).join("") : `<div class="empty">${escapeHtml(emptyText)}</div>`;
    };
    const palette = ["#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#a855f7", "#06b6d4", "#f97316", "#84cc16"];
    const C = 2 * Math.PI * 42; // SVG circumference for r=42
    const consolidateRows = (rawRows, maxSlices) => {
      const sorted = [...rawRows].sort((a, b) => Number(b.raw_value || 0) - Number(a.raw_value || 0));
      if (sorted.length <= maxSlices) return sorted;
      const main = sorted.slice(0, maxSlices - 1);
      const rest = sorted.slice(maxSlices - 1);
      const pct = Math.round(rest.reduce((s, r) => s + Number(r.percent || 0), 0) * 10) / 10;
      return [...main, { label: "Other", percent: pct, raw_value: 0, value: "" }];
    };
    const renderPieChart = (rawRows, maxSlices = 7) => {
      if (!rawRows.length) return `<div class="empty">No breakdown data.</div>`;
      const rows = consolidateRows(rawRows, maxSlices).filter(r => Number(r.percent || 0) > 0);
      if (!rows.length) return `<div class="empty">No breakdown data.</div>`;
      let cumPct = 0;
      const circles = rows.map((row, i) => {
        const pct = Number(row.percent || 0);
        const dash = (pct / 100 * C).toFixed(2);
        const gap = (C - pct / 100 * C).toFixed(2);
        const off = (-(cumPct / 100) * C).toFixed(2);
        cumPct += pct;
        const hint = row.value ? ` · ${row.value}` : "";
        return `<circle r="42" cx="50" cy="50" fill="transparent" stroke="${palette[i % palette.length]}" stroke-width="16" stroke-dasharray="${dash} ${gap}" stroke-dashoffset="${off}" transform="rotate(-90 50 50)"><title>${row.label}: ${row.percent}%${hint}</title></circle>`;
      }).join("");
      const legend = rows.map((row, i) => `
        <span><span class="legend-dot" style="background:${palette[i % palette.length]}"></span>${escapeHtml(row.label)} ${row.percent}%</span>
      `).join("");
      return `
        <svg class="pie-chart" viewBox="0 0 100 100" role="img" aria-label="Breakdown chart" style="cursor:default">
          <circle r="42" cx="50" cy="50" fill="transparent" stroke="#eef1f5" stroke-width="16"/>
          ${circles}
        </svg>
        <div class="chart-legend">${legend}</div>
      `;
    };
    const renderDetailBars = rows => {
      if (!rows.length) return `<div class="empty">No line-item data yet.</div>`;
      const max = Math.max(...rows.map(row => Number(row.raw_value || 0)), 1);
      return `<div class="detail-bars">${rows.map(row => `
        <div class="detail-bar-row">
          <div class="row-title">${escapeHtml(row.label)}</div>
          <div class="bar-track"><div class="bar" style="width:${Math.max(4, Number(row.raw_value || 0) / max * 100)}%"></div></div>
          <div class="amount">${escapeHtml(row.value)}</div>
        </div>
      `).join("")}</div>`;
    };
    const renderExpenseRows = rows => rows.length ? rows.map(row => `
      <div class="row">
        <div class="row-main">
          <div class="row-title">${escapeHtml(row.description)}</div>
          <div class="row-sub">${escapeHtml(row.date)}${row.merchant ? ` · ${escapeHtml(row.merchant)}` : ""}${row.subcategory ? ` · ${escapeHtml(row.subcategory)}` : ` · ${escapeHtml(row.source)}`}</div>
        </div>
        <div class="amount">${escapeHtml(row.amount)}</div>
      </div>
    `).join("") : `<div class="empty">No expenses in this category for the selected period.</div>`;
    const openCategoryModal = category => {
      const details = latestDashboardData?.category_details?.[category];
      if (!details) return;
      document.querySelector("#category-modal-title").textContent = category;
      document.querySelector("#category-modal-subtitle").textContent = latestDashboardData.period.label;
      if (category === "Food") {
        document.querySelector("#category-modal-body").innerHTML = `
          <div class="modal-grid">
            <section class="mini-panel">
              <h2>By Supermarket</h2>
              ${renderPieChart(details.store_breakdown || [], 5)}
            </section>
            <section class="mini-panel">
              <h2>By Food Type</h2>
              ${renderPieChart(details.nutrient_breakdown || [], 8)}
            </section>
          </div>
          <section class="mini-panel">
            <h2>Food Type Values</h2>
            ${renderDetailBars(details.nutrient_breakdown || [])}
          </section>
          <section class="mini-panel" style="margin-top:14px">
            <h2>Food Expenses</h2>
            <div class="rows">${renderExpenseRows(details.expenses || [])}</div>
          </section>
        `;
      } else if (category === "Commute") {
        document.querySelector("#category-modal-body").innerHTML = `
          <div class="modal-grid">
            <section class="mini-panel">
              <h2>By Type</h2>
              ${renderPieChart(details.subcategory_breakdown || [], 5)}
            </section>
            <section class="mini-panel">
              <h2>Breakdown</h2>
              ${renderDetailBars(details.subcategory_breakdown || [])}
            </section>
          </div>
          <section class="mini-panel" style="margin-top:14px">
            <h2>Commute Expenses</h2>
            <div class="rows">${renderExpenseRows(details.expenses || [])}</div>
          </section>
        `;
      } else {
        document.querySelector("#category-modal-body").innerHTML = `
          <section class="mini-panel">
            <h2>Expenses</h2>
            <div class="rows">${renderExpenseRows(details.expenses || [])}</div>
          </section>
        `;
      }
      document.querySelector("#category-modal").classList.add("open");
      document.querySelector("#category-modal").setAttribute("aria-hidden", "false");
    };
    const closeCategoryModal = () => {
      document.querySelector("#category-modal").classList.remove("open");
      document.querySelector("#category-modal").setAttribute("aria-hidden", "true");
    };
    const openIncomeModal = () => {
      const transactions = latestDashboardData?.income_transactions || [];
      document.querySelector("#income-modal-title").textContent = `Income ${latestDashboardData?.period?.label || ""}`;
      document.querySelector("#income-modal-subtitle").textContent = transactions.length
        ? `${transactions.length} transaction${transactions.length === 1 ? "" : "s"}`
        : "No income recorded";
      if (transactions.length === 0) {
        document.querySelector("#income-modal-body").innerHTML = `<div class="empty">No income transactions recorded for this period.</div>`;
      } else {
        document.querySelector("#income-modal-body").innerHTML = `
          <div class="rows">
            ${transactions.map(tx => `
              <div class="row">
                <div class="row-main">
                  <div class="row-title">${escapeHtml(tx.description)}${tx.merchant && tx.merchant !== tx.description ? ` <span class="muted">(${escapeHtml(tx.merchant)})</span>` : ""}</div>
                  <div class="row-sub">${escapeHtml(tx.category)} &middot; ${escapeHtml(tx.date)}</div>
                </div>
                <div class="row-aside income-amount">${escapeHtml(tx.amount)}</div>
              </div>
            `).join("")}
          </div>
        `;
      }
      document.querySelector("#income-modal").classList.add("open");
      document.querySelector("#income-modal").setAttribute("aria-hidden", "false");
    };
    const closeIncomeModal = () => {
      document.querySelector("#income-modal").classList.remove("open");
      document.querySelector("#income-modal").setAttribute("aria-hidden", "true");
    };
    async function loadCalendarSettings() {
      const status = document.querySelector("#calendar-settings-status");
      status.textContent = "Loading";
      try {
        const response = await fetch("/api/calendar/settings");
        if (!response.ok) {
          status.textContent = await responseErrorMessage(response, "Could not load calendar settings.");
          toast("Could not load calendar settings.");
          return;
        }
        const data = await response.json();
        const connected = Boolean(data.connected);
        document.querySelector("#google-calendar-id").value = data.google_calendar_id || "primary";
        updateCalendarEmbed(data.google_calendar_id || "primary");
        document.querySelector("#calendar-modal-subtitle").textContent = connected
          ? `${data.connection_count} Google connection${data.connection_count === 1 ? "" : "s"} connected`
          : "Google is not connected yet";
        document.querySelector("#calendar-connect-link").classList.toggle("hidden", connected);
        document.querySelector("#calendar-sync-now").disabled = !connected;
        status.textContent = connected ? "" : "Save the calendar ID, then connect Google.";
      } catch {
        status.textContent = "Could not load calendar settings.";
        toast("Could not load calendar settings.");
      }
    }
    function updateCalendarEmbed(calendarId) {
      const calendarText = String(calendarId || "").trim();
      const iframe = document.querySelector("#google-calendar-embed");
      const status = document.querySelector("#calendar-embed-status");
      if (!calendarText) {
        iframe.removeAttribute("src");
        status.textContent = "No calendar ID configured";
        return;
      }
      const params = new URLSearchParams({
        src: calendarText,
        ctz: "Europe/Lisbon",
        mode: "WEEK",
        showTitle: "0",
        showPrint: "0",
        showTabs: "1",
        showCalendars: "0",
      });
      iframe.src = `https://calendar.google.com/calendar/embed?${params.toString()}`;
      status.textContent = "Live Google view";
    }
    const selectedCalendarDay = () => {
      const input = document.querySelector("#calendar-day-date");
      const normalized = normalizeDayValue(input?.value || "");
      return normalized || new Date().toISOString().slice(0, 10);
    };
    async function loadCalendarModalAgenda() {
      const container = document.querySelector("#calendar-day-events");
      container.innerHTML = `<div class="empty">Loading events.</div>`;
      try {
        const response = await fetch(`/api/tasks/day?day=${encodeURIComponent(selectedCalendarDay())}`);
        if (!response.ok) {
          container.innerHTML = `<div class="empty">Could not load calendar events.</div>`;
          toast("Could not load calendar events.");
          return;
        }
        const data = await response.json();
        const events = Array.isArray(data.events) ? data.events : [];
        container.innerHTML = events.length ? events.map(item => `
          <div class="event-row">
            <div class="row-title">${escapeHtml(item.title)}</div>
            <div class="row-sub">${item.all_day ? "All day" : `${escapeHtml(item.start)}-${escapeHtml(item.end)}`} · ${escapeHtml(item.source || "event")}</div>
          </div>
        `).join("") : `<div class="empty">No synced events for this day. The live Google view below can still show events before Sync now succeeds.</div>`;
      } catch {
        container.innerHTML = `<div class="empty">Could not load calendar events.</div>`;
        toast("Could not load calendar events.");
      }
    }
    const openCalendarModal = async () => {
      const calendarDay = document.querySelector("#calendar-day-date");
      if (!calendarDay.value) calendarDay.value = new Date().toISOString().slice(0, 10);
      document.querySelector("#calendar-modal").classList.add("open");
      document.querySelector("#calendar-modal").setAttribute("aria-hidden", "false");
      await Promise.all([loadCalendarSettings(), loadCalendarModalAgenda()]);
    };
    const closeCalendarModal = () => {
      document.querySelector("#calendar-modal").classList.remove("open");
      document.querySelector("#calendar-modal").setAttribute("aria-hidden", "true");
    };
    const renderCategoryOptions = (categories, selected) => categories.map(category => (
      `<option value="${escapeHtml(category)}" ${category === selected ? "selected" : ""}>${escapeHtml(category)}</option>`
    )).join("");
    const toast = message => {
      const el = document.querySelector("#toast");
      el.textContent = message;
      el.classList.add("show");
      window.clearTimeout(window.toastTimer);
      window.toastTimer = window.setTimeout(() => el.classList.remove("show"), 2400);
    };
    async function responseErrorMessage(response, fallback) {
      try {
        const data = await response.json();
        return data.detail || data.error || fallback;
      } catch {
        return fallback;
      }
    }
    function showCalendarStatusFromUrl() {
      const params = new URLSearchParams(window.location.search);
      const calendarStatus = params.get("calendar");
      if (!calendarStatus) return;
      const messages = {
        connected: "Google Calendar connected.",
        "connected-sync-failed": "Google Calendar connected, but the first sync failed. Check the calendar ID and press Sync now.",
        "auth-failed": "Google Calendar authorization failed. Please try Connect Google again.",
        "auth-request-failed": "Google Calendar authorization could not reach Google. Please try again.",
      };
      toast(messages[calendarStatus] || "Calendar connection finished.");
      params.delete("calendar");
      const cleanQuery = params.toString();
      const cleanUrl = `${window.location.pathname}${cleanQuery ? `?${cleanQuery}` : ""}${window.location.hash}`;
      window.history.replaceState({}, "", cleanUrl);
    }

    async function loadDashboard() {
      document.querySelector("#status").textContent = "Updating";
      const response = await fetch(`/api/dashboard?${dashboardQuery()}`);
      const data = await response.json();
      latestDashboardData = data;
      if (data.error) {
        document.querySelector(".shell").innerHTML = `<div class="panel">${escapeHtml(data.error)}</div>`;
        return;
      }

      document.querySelector("#month-filter").value = data.period.month;
      const t = data.totals;
      document.querySelector("#metrics").innerHTML = [
        metric(`Expenses ${data.period.label}`, t.this_month),
        clickableMetric(`Income ${data.period.label}`, t.income_month, "income-breakdown"),
        metric(data.totals.saved_month_value >= 0 ? "Saved So Far" : "Over So Far", `${Math.abs(moneyNumber(t.saved_month)).toFixed(2)} EUR`),
        data.period.is_current_month
          ? metric("Next Month (Projection)", t.next_month_projection)
          : metric("Receipts", String(t.receipt_count_month)),
      ].join("");
      document.querySelector("#task-metrics").innerHTML = [
        metric("Completion", `${data.task_stats.completion_rate}%`),
        metric("Done", String(data.task_stats.done)),
        metric("Skipped", String(data.task_stats.skipped)),
        metric("Moved", String(data.task_stats.moved)),
      ].join("");

      document.querySelector("#expense-categories").innerHTML = renderBars(data.expense_categories, `No expenses logged for ${data.period.label}.`);

      document.querySelector("#cashflow-bars").innerHTML = renderCurrentCashflow(t);
      document.querySelector("#cashflow-line").innerHTML = renderMonthlyCashflow(data.monthly_cashflow);

      document.querySelector("#recommendations").innerHTML = data.recommendations
        .map(item => `<div class="recommendation">${escapeHtml(item)}</div>`)
        .join("");

      const totalShoppingItems = (data.shopping || []).reduce((s, g) => s + (g.items || []).length, 0);
      setCollapseButton("categories", totalShoppingItems, false);
      const activeShoppingGroups = (data.shopping || []).filter(g => g.items && g.items.length > 0);
      if (!activeShoppingGroups.length) {
        document.querySelector("#categories").innerHTML = `<div class="empty">No pending shopping items.</div>`;
      } else {
        let shoppingRowCount = 0;
        document.querySelector("#categories").innerHTML = activeShoppingGroups.map(group => {
          const rows = group.items.map(item => {
            const hidden = shoppingRowCount++ >= 6 ? " hidden-row" : "";
            return `
              <div class="row${hidden}" data-collapsible-row="categories">
                <div class="row-main">
                  <div class="row-title">${escapeHtml(item.name)} <span class="muted">(${escapeHtml(item.store)})</span></div>
                  <div class="price-meta">
                    ${item.product_name ? `<span>${escapeHtml(item.product_name)}</span>` : `<span>No online price checked yet</span>`}
                    ${item.price_store ? `<span>· cheapest at ${escapeHtml(item.price_store)}</span>` : ""}
                    ${item.old_price ? `<span class="old-price">${escapeHtml(item.old_price)}</span>` : ""}
                    ${item.is_promotion === "yes" ? `<span class="promo">promotion</span>` : ""}
                    <button class="link-btn icon-btn" type="button" data-price-toggle="${escapeHtml(item.id)}" title="${item.price ? 'Edit price' : 'Add price'}" aria-label="${item.price ? 'Edit price' : 'Add price'}">${iconSvg(item.price ? "edit" : "add")}</button>
                  </div>
                  <form class="price-form" data-price-form="${escapeHtml(item.id)}">
                    <input name="store_name" placeholder="Store" value="${escapeHtml(item.store !== "anywhere" && item.store !== "online" ? item.store : "")}">
                    <input name="price" inputmode="decimal" placeholder="Price">
                    <button type="submit">Save</button>
                  </form>
                </div>
                <div class="transaction-actions">
                  ${item.price ? `<div class="amount">${escapeHtml(item.price)}</div>` : ""}
                  <button class="delete-btn" type="button" aria-label="Remove shopping item" title="Remove shopping item" data-shopping-delete="${escapeHtml(item.id)}">${iconSvg("delete")}</button>
                </div>
              </div>`;
          }).join("");
          return `<div class="category"><div class="category-title">${escapeHtml(group.category)}</div><div class="rows">${rows}</div></div>`;
        }).join("");
      }

      setCollapseButton("promotions", data.promotions.length, false);
      document.querySelector("#promotions").innerHTML = data.promotions.length ? data.promotions.map((item, index) => `
        <div class="row ${index >= 6 ? "hidden-row" : ""}" data-collapsible-row="promotions">
          <div class="row-main">
            <div class="row-title">${escapeHtml(item.item_name)} <span class="muted">(${escapeHtml(item.store_name)})</span></div>
            <div class="price-meta">
              <span>${escapeHtml(item.product_name)}</span>
              ${item.old_price ? `<span class="old-price">${escapeHtml(item.old_price)}</span>` : ""}
              <span class="promo">promotion</span>
            </div>
          </div>
          <div class="amount">${escapeHtml(item.price)}</div>
        </div>
      `).join("") : `<div class="empty">No promotions found yet for products from your receipt history.</div>`;

      document.querySelector("#receipt-count").textContent = data.recent_receipts.length
        ? `${data.recent_receipts.length} shown`
        : "";
      document.querySelector("#receipts").innerHTML = data.recent_receipts.length ? data.recent_receipts.map(r => `
        <div class="row">
          <div class="row-main">
            <div class="row-title">${escapeHtml(r.shop)}</div>
            <div class="row-sub">${escapeHtml(r.date)}</div>
          </div>
          <div class="amount">${escapeHtml(r.total)}</div>
          <button class="delete-btn" type="button" aria-label="Delete receipt" title="Delete receipt" data-receipt-id="${escapeHtml(r.id)}">
            <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
              <path d="M9 3h6l1 2h4v2H4V5h4l1-2Zm1 7h2v8h-2v-8Zm4 0h2v8h-2v-8ZM6 9h12l-1 12H7L6 9Z" fill="currentColor"/>
            </svg>
          </button>
        </div>
      `).join("") : `<div class="empty">No confirmed receipts yet.</div>`;

      setCollapseButton("transactions", data.transactions.length, false);
      document.querySelector("#transactions").innerHTML = data.transactions.length ? data.transactions.map((item, index) => `
        <div class="row ${index >= 6 ? "hidden-row" : ""}" data-collapsible-row="transactions">
          <div class="row-main">
            <div class="row-title">${escapeHtml(item.description)}</div>
            <div class="row-sub">${escapeHtml(item.date)} · ${escapeHtml(item.category)}${item.merchant ? ` · ${escapeHtml(item.merchant)}` : ""}</div>
          </div>
          <div class="transaction-actions">
            <select class="category-select" data-transaction-category="${escapeHtml(item.id)}" aria-label="Expense category">
              ${renderCategoryOptions(data.finance_categories || [], item.category)}
            </select>
            <div class="amount">-${escapeHtml(item.amount)}</div>
            <button class="delete-btn" type="button" aria-label="Delete transaction" title="Delete transaction" data-transaction-id="${escapeHtml(item.id)}">
              <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
                <path d="M9 3h6l1 2h4v2H4V5h4l1-2Zm1 7h2v8h-2v-8Zm4 0h2v8h-2v-8ZM6 9h12l-1 12H7L6 9Z" fill="currentColor"/>
              </svg>
            </button>
          </div>
        </div>
      `).join("") : `<div class="empty">No expense transactions logged this month.</div>`;

      document.querySelector("#activity").innerHTML = data.activity.length ? data.activity.map(item => `
        <div class="row">
          <div class="row-main">
            <div class="row-title">${escapeHtml(item.summary)}</div>
            <div class="row-sub">${escapeHtml(item.actor)} · <span class="activity-pill ${escapeHtml(item.activity_class || "")}">${escapeHtml(item.entity_type)}</span> · ${escapeHtml(item.action)}${item.category ? ` · ${escapeHtml(item.category)}` : ""}</div>
          </div>
        </div>
      `).join("") : `<div class="empty">No household activity yet.</div>`;

      document.querySelector("#status").textContent = "Updated just now";
    }

    const setSelectOptions = (select, values, label) => {
      const current = select.value;
      select.innerHTML = `<option value="">${escapeHtml(label)}</option>` + values.map(value => (
        `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`
      )).join("");
      select.value = current;
    };

    async function loadActivity(page = activityPage) {
      activityPage = page;
      const form = document.querySelector("#activity-filters");
      const params = new URLSearchParams({ page: String(activityPage), page_size: "20" });
      new FormData(form).forEach((value, key) => {
        if (String(value).trim()) params.set(key, String(value).trim());
      });
      const response = await fetch(`/api/activity?${params.toString()}`);
      const data = await response.json();
      setSelectOptions(form.elements.entity_type, data.entity_types || [], "All types");
      setSelectOptions(form.elements.action, data.actions || [], "All actions");
      setSelectOptions(form.elements.category, data.categories || [], "All categories");
      document.querySelector("#activity").innerHTML = data.items.length ? data.items.map(item => `
        <div class="row">
          <div class="row-main">
            <div class="row-title">${escapeHtml(item.summary)}</div>
            <div class="row-sub">${escapeHtml(item.actor)} · <span class="activity-pill ${escapeHtml(item.activity_class || "")}">${escapeHtml(item.entity_type)}</span> · ${escapeHtml(item.action)}${item.category ? ` · ${escapeHtml(item.category)}` : ""} · ${escapeHtml(new Date(item.date).toLocaleString())}</div>
          </div>
        </div>
      `).join("") : `<div class="empty">No activity matches these filters.</div>`;
      document.querySelector("#activity-page").textContent = `Page ${data.page} of ${data.pages} · ${data.total} results`;
      document.querySelector("#activity-prev").disabled = data.page <= 1;
      document.querySelector("#activity-next").disabled = data.page >= data.pages;
    }

    async function loadRoutines() {
      const response = await fetch("/api/routines");
      const data = await response.json();
      document.querySelector("#routines").innerHTML = data.items && data.items.length ? data.items.map(item => `
        <form class="routine-row" data-routine-id="${escapeHtml(item.id)}">
          <input name="title" value="${escapeHtml(item.title)}" aria-label="Task name">
          <input name="duration_minutes" type="number" min="1" max="360" value="${escapeHtml(item.duration_minutes)}" aria-label="Minimum minutes">
          <input name="duration_max" type="number" min="1" max="360" value="${escapeHtml(item.duration_max)}" aria-label="Maximum minutes">
          <label class="check-label"><input name="is_active" type="checkbox" ${item.is_active ? "checked" : ""}> Active</label>
          <div>
            <button class="primary-btn" type="submit">Save</button>
            <button class="delete-btn" type="button" data-routine-delete="${escapeHtml(item.id)}" aria-label="Delete must task" title="Delete must task">
              <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
                <path d="M9 3h6l1 2h4v2H4V5h4l1-2Zm1 7h2v8h-2v-8Zm4 0h2v8h-2v-8ZM6 9h12l-1 12H7L6 9Z" fill="currentColor"/>
              </svg>
            </button>
          </div>
        </form>
      `).join("") : `<div class="empty">No must tasks configured.</div>`;
    }

    const normalizeDayValue = value => {
      const raw = String(value || "").trim();
      if (!raw) return "";
      if (/^\\d{4}-\\d{2}-\\d{2}$/.test(raw)) return raw;
      const slash = raw.match(/^(\\d{1,2})[/](\\d{1,2})[/](\\d{4})$/);
      if (slash) {
        const day = slash[1].padStart(2, "0");
        const month = slash[2].padStart(2, "0");
        return `${slash[3]}-${month}-${day}`;
      }
      const dash = raw.match(/^(\\d{1,2})-(\\d{1,2})-(\\d{4})$/);
      if (dash) {
        const day = dash[1].padStart(2, "0");
        const month = dash[2].padStart(2, "0");
        return `${dash[3]}-${month}-${day}`;
      }
      return raw;
    };

    const selectedDay = () => {
      const input = document.querySelector("#day-task-date");
      const normalized = normalizeDayValue(input?.value || "");
      return normalized || new Date().toISOString().slice(0, 10);
    };

    async function loadDayAgenda() {
      const response = await fetch(`/api/tasks/day?day=${encodeURIComponent(selectedDay())}`);
      if (!response.ok) {
        toast("Could not load tasks/events for this day.");
        return;
      }
      const data = await response.json();
      const tasks = Array.isArray(data.items) ? data.items : [];
      const events = Array.isArray(data.events) ? data.events : [];

      document.querySelector("#day-tasks").innerHTML = tasks.length ? tasks.map(item => `
        <div class="day-task-row">
          <div class="row-main">
            <div class="row-title">${escapeHtml(item.title)}</div>
            <div class="row-sub">${item.is_must ? "Must task" : (item.due_date ? `Due ${escapeHtml(item.due_date)}` : "No due date")}</div>
          </div>
          <div class="day-task-actions">
            ${item.is_must ? "" : `<button class="mini-btn" type="button" data-day-task-move="${escapeHtml(item.id)}">Move</button>`}
            <button class="delete-btn" type="button" data-day-task-delete="${escapeHtml(item.id)}" aria-label="Remove task" title="Remove task">
              <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
                <path d="M9 3h6l1 2h4v2H4V5h4l1-2Zm1 7h2v8h-2v-8Zm4 0h2v8h-2v-8ZM6 9h12l-1 12H7L6 9Z" fill="currentColor"/>
              </svg>
            </button>
          </div>
        </div>
      `).join("") : `<div class="empty">No tasks for this day.</div>`;

      document.querySelector("#day-events").innerHTML = events.length ? events.map(item => `
        <div class="event-row">
          <div class="row-title">${escapeHtml(item.title)}</div>
          <div class="row-sub">${item.all_day ? "All day" : `${escapeHtml(item.start)}-${escapeHtml(item.end)}`} · ${escapeHtml(item.source || "event")}</div>
          ${item.editable ? `
            <div class="event-actions">
              <button class="mini-btn" type="button" data-day-event-move="${escapeHtml(item.event_ref || "")}">Move</button>
              <button class="delete-btn" type="button" data-day-event-delete="${escapeHtml(item.event_ref || "")}" aria-label="Delete event" title="Delete event">
                <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
                  <path d="M9 3h6l1 2h4v2H4V5h4l1-2Zm1 7h2v8h-2v-8Zm4 0h2v8h-2v-8ZM6 9h12l-1 12H7L6 9Z" fill="currentColor"/>
                </svg>
              </button>
            </div>
          ` : ""}
        </div>
      `).join("") : `<div class="empty">No events for this day.</div>`;
    }

    async function loadDefaults() {
      const response = await fetch("/api/planning-defaults");
      if (!response.ok) {
        toast("Could not load defaults.");
        return;
      }
      const data = await response.json();
      const form = document.querySelector("#defaults-form");
      Object.entries(data).forEach(([key, value]) => {
        if (form.elements[key]) form.elements[key].value = value || "";
      });
    }

    document.addEventListener("click", async event => {
      const modalClose = event.target.closest("#category-modal-close");
      if (modalClose || event.target.id === "category-modal") {
        closeCategoryModal();
        return;
      }

      const incomeModalClose = event.target.closest("#income-modal-close");
      if (incomeModalClose || event.target.id === "income-modal") {
        closeIncomeModal();
        return;
      }

      const calendarModalClose = event.target.closest("#calendar-modal-close");
      if (calendarModalClose || event.target.id === "calendar-modal") {
        closeCalendarModal();
        return;
      }

      const calendarOpen = event.target.closest("#calendar-settings-open");
      if (calendarOpen) {
        await openCalendarModal();
        return;
      }

      const calendarDayRefresh = event.target.closest("#calendar-refresh-day");
      if (calendarDayRefresh) {
        await loadCalendarModalAgenda();
        return;
      }

      const calendarSync = event.target.closest("#calendar-sync-now");
      if (calendarSync) {
        if (calendarSync.disabled) return;
        calendarSync.disabled = true;
        document.querySelector("#calendar-settings-status").textContent = "Syncing";
        try {
          const response = await fetch("/api/calendar/sync", { method: "POST" });
          if (!response.ok) {
            const message = await responseErrorMessage(response, "Could not sync calendar.");
            document.querySelector("#calendar-settings-status").textContent = message;
            toast(message);
            return;
          }
          const result = await response.json();
          toast(`Synced ${result.google_events} Google event(s), ${result.ical_events} iCal event(s).`);
          document.querySelector("#calendar-settings-status").textContent = "Calendar synced.";
          await loadDayAgenda();
          await loadCalendarModalAgenda();
        } catch {
          document.querySelector("#calendar-settings-status").textContent = "Could not sync calendar.";
          toast("Could not sync calendar.");
        } finally {
          const connected = document.querySelector("#calendar-connect-link").classList.contains("hidden");
          calendarSync.disabled = !connected;
        }
        return;
      }

      const metricAction = event.target.closest("[data-metric-action]");
      if (metricAction) {
        const action = metricAction.dataset.metricAction;
        if (action === "income-breakdown") {
          openIncomeModal();
          return;
        }
      }

      const categoryDetail = event.target.closest("[data-category-detail]");
      if (categoryDetail) {
        openCategoryModal(categoryDetail.dataset.categoryDetail);
        return;
      }

      const tabButton = event.target.closest("[data-view-target]");
      if (tabButton) {
        document.querySelectorAll(".tab-btn").forEach(button => button.classList.remove("active"));
        tabButton.classList.add("active");
        document.querySelectorAll(".view").forEach(view => view.classList.add("hidden"));
        document.querySelector(`#${tabButton.dataset.viewTarget}`).classList.remove("hidden");
        document.querySelector("#metrics").classList.toggle("hidden", tabButton.dataset.viewTarget !== "overview-view");
        if (tabButton.dataset.viewTarget === "activity-view") loadActivity(1);
        if (tabButton.dataset.viewTarget === "tasks-view") {
          loadRoutines();
          loadDefaults();
          loadDayAgenda();
        }
        return;
      }

      const collapseButton = event.target.closest("[data-collapse-target]");
      if (collapseButton) {
        const target = collapseButton.dataset.collapseTarget;
        const rows = Array.from(document.querySelectorAll(`[data-collapsible-row="${target}"]`));
        const expanded = collapseButton.dataset.expanded === "true";
        rows.forEach((row, index) => {
          if (index >= 6) row.classList.toggle("hidden-row", expanded);
        });
        collapseButton.dataset.expanded = expanded ? "false" : "true";
        collapseButton.textContent = expanded ? `Show all ${rows.length}` : "Show less";
        return;
      }

      const priceToggle = event.target.closest("[data-price-toggle]");
      if (priceToggle) {
        const form = document.querySelector(`[data-price-form="${priceToggle.dataset.priceToggle}"]`);
        if (form) form.classList.toggle("open");
        return;
      }

      const shoppingDelete = event.target.closest("[data-shopping-delete]");
      if (shoppingDelete) {
        const confirmed = window.confirm("Remove this item from the shopping list?");
        if (!confirmed) return;
        shoppingDelete.disabled = true;
        const response = await fetch(`/api/shopping/items/${shoppingDelete.dataset.shoppingDelete}`, { method: "DELETE" });
        if (!response.ok) {
          shoppingDelete.disabled = false;
          toast("Could not remove shopping item.");
          return;
        }
        toast("Shopping item removed.");
        await loadDashboard();
        return;
      }

      const refreshPrices = event.target.closest("#refresh-prices");
      if (refreshPrices) {
        refreshPrices.disabled = true;
        refreshPrices.textContent = "Checking";
        const response = await fetch("/api/shopping/prices/refresh", { method: "POST" });
        if (!response.ok) {
          toast("Could not check prices.");
        } else {
          const result = await response.json();
          toast(`Saved ${result.saved} price quote(s).`);
          await loadDashboard();
        }
        refreshPrices.disabled = false;
        refreshPrices.textContent = "Check prices";
        return;
      }

      const routineDelete = event.target.closest("[data-routine-delete]");
      if (routineDelete) {
        const confirmed = window.confirm("Delete this must task for the household?");
        if (!confirmed) return;
        const response = await fetch(`/api/routines/${routineDelete.dataset.routineDelete}`, { method: "DELETE" });
        if (!response.ok) {
          toast("Could not delete must task.");
          return;
        }
        toast("Must task deleted.");
        await loadRoutines();
        return;
      }

      const dayTaskDelete = event.target.closest("[data-day-task-delete]");
      if (dayTaskDelete) {
        const confirmed = window.confirm("Remove this task from the selected day list?");
        if (!confirmed) return;
        const response = await fetch(`/api/tasks/${dayTaskDelete.dataset.dayTaskDelete}`, { method: "DELETE" });
        if (!response.ok) {
          toast("Could not remove task.");
          return;
        }
        toast("Task removed.");
        await loadDayAgenda();
        return;
      }

      const dayTaskMove = event.target.closest("[data-day-task-move]");
      if (dayTaskMove) {
        const suggested = selectedDay();
        const nextDate = window.prompt("Move task to date (YYYY-MM-DD)", suggested);
        if (!nextDate) return;
        const response = await fetch(`/api/tasks/${dayTaskMove.dataset.dayTaskMove}/move`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ due_date: nextDate }),
        });
        if (!response.ok) {
          toast("Could not move task. Use YYYY-MM-DD.");
          return;
        }
        toast("Task moved.");
        await loadDayAgenda();
        return;
      }

      const dayEventDelete = event.target.closest("[data-day-event-delete]");
      if (dayEventDelete) {
        const confirmed = window.confirm("Delete this event from the selected day?");
        if (!confirmed) return;
        const response = await fetch(
          `/api/events/day?day=${encodeURIComponent(selectedDay())}&event_ref=${encodeURIComponent(dayEventDelete.dataset.dayEventDelete)}`,
          { method: "DELETE" },
        );
        if (!response.ok) {
          toast("Could not delete event.");
          return;
        }
        toast("Event deleted.");
        await loadDayAgenda();
        return;
      }

      const dayEventMove = event.target.closest("[data-day-event-move]");
      if (dayEventMove) {
        const nextDate = window.prompt("Move event to date (YYYY-MM-DD)", selectedDay());
        if (!nextDate) return;
        const response = await fetch("/api/events/day/move", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            day: selectedDay(),
            event_ref: dayEventMove.dataset.dayEventMove,
            target_day: nextDate,
          }),
        });
        if (!response.ok) {
          toast("Could not move event. Use YYYY-MM-DD.");
          return;
        }
        toast("Event moved.");
        await loadDayAgenda();
        return;
      }

      const transactionDeleteBtn = event.target.closest("[data-transaction-id]");
      if (transactionDeleteBtn) {
        const transactionId = transactionDeleteBtn.dataset.transactionId;
        const confirmed = window.confirm("Delete this bank transaction? You can then re-add it as a receipt for item-level tracking.");
        if (!confirmed) return;
        transactionDeleteBtn.disabled = true;
        const response = await fetch(`/api/transactions/${transactionId}`, { method: "DELETE" });
        if (!response.ok) {
          transactionDeleteBtn.disabled = false;
          toast("Could not delete transaction.");
          return;
        }
        toast("Transaction deleted. You can now add the receipt via the bot.");
        await loadDashboard();
        return;
      }

      const button = event.target.closest("[data-receipt-id]");
      if (!button) return;
      const receiptId = button.dataset.receiptId;
      const confirmed = window.confirm("Delete this receipt and all extracted receipt items from this household account?");
      if (!confirmed) return;

      button.disabled = true;
      const response = await fetch(`/api/receipts/${receiptId}`, { method: "DELETE" });
      if (!response.ok) {
        button.disabled = false;
        toast("Could not delete receipt.");
        return;
      }
      toast("Receipt deleted.");
      await loadDashboard();
    });

    document.addEventListener("change", async event => {
      const categorySelect = event.target.closest("[data-transaction-category]");
      if (!categorySelect) return;
      categorySelect.disabled = true;
      const response = await fetch(`/api/transactions/${categorySelect.dataset.transactionCategory}/category`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ category: categorySelect.value }),
      });
      if (!response.ok) {
        categorySelect.disabled = false;
        toast("Could not update category.");
        return;
      }
      toast("Category updated.");
      await loadDashboard();
    });

    document.addEventListener("submit", async event => {
      if (event.target.id === "activity-filters") {
        event.preventDefault();
        await loadActivity(1);
        return;
      }
      if (event.target.id === "routine-create") {
        event.preventDefault();
        const formData = new FormData(event.target);
        const response = await fetch("/api/routines", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: formData.get("title"),
            duration_minutes: Number(formData.get("duration_minutes") || 30),
            duration_max: Number(formData.get("duration_max") || formData.get("duration_minutes") || 30),
            is_active: formData.get("is_active") === "on",
          }),
        });
        if (!response.ok) {
          toast("Could not add must task.");
          return;
        }
        event.target.reset();
        event.target.elements.is_active.checked = true;
        toast("Must task added.");
        await loadRoutines();
        return;
      }
      if (event.target.id === "day-task-create") {
        event.preventDefault();
        const formData = new FormData(event.target);
        const response = await fetch("/api/tasks", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: formData.get("title"),
            due_date: selectedDay(),
          }),
        });
        if (!response.ok) {
          toast("Could not add day item.");
          return;
        }
        const result = await response.json();
        event.target.reset();
        toast(result.kind === "event" ? "Event added." : "Task added.");
        await loadDayAgenda();
        return;
      }
      if (event.target.id === "defaults-form") {
        event.preventDefault();
        const formData = new FormData(event.target);
        const payload = {};
        [
          "work_start", "work_end", "wake_time", "bed_time",
          "commute_start", "commute_end",
          "breakfast_start", "breakfast_end",
          "lunch_start", "lunch_end",
          "dinner_start", "dinner_end",
        ].forEach(key => {
          payload[key] = String(formData.get(key) || "").trim() || null;
        });
        const response = await fetch("/api/planning-defaults", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!response.ok) {
          toast("Could not save defaults.");
          return;
        }
        toast("Defaults saved.");
        await loadDayAgenda();
        return;
      }
      if (event.target.id === "calendar-settings-form") {
        event.preventDefault();
        const formData = new FormData(event.target);
        const saveButton = event.target.querySelector('button[type="submit"]');
        saveButton.disabled = true;
        document.querySelector("#calendar-settings-status").textContent = "Saving";
        try {
          const response = await fetch("/api/calendar/settings", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              google_calendar_id: formData.get("google_calendar_id"),
            }),
          });
          if (!response.ok) {
            const message = await responseErrorMessage(response, "Could not save calendar ID.");
            document.querySelector("#calendar-settings-status").textContent = message;
            toast(message);
            return;
          }
          const result = await response.json();
          document.querySelector("#google-calendar-id").value = result.google_calendar_id;
          updateCalendarEmbed(result.google_calendar_id);
          toast("Calendar ID saved.");
          document.querySelector("#calendar-settings-status").textContent = "Calendar ID saved. Connect Google next.";
          await loadCalendarSettings();
        } catch {
          document.querySelector("#calendar-settings-status").textContent = "Could not save calendar ID.";
          toast("Could not save calendar ID.");
        } finally {
          saveButton.disabled = false;
        }
        return;
      }
      const routineForm = event.target.closest("[data-routine-id]");
      if (routineForm) {
        event.preventDefault();
        const formData = new FormData(routineForm);
        const response = await fetch(`/api/routines/${routineForm.dataset.routineId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: formData.get("title"),
            duration_minutes: Number(formData.get("duration_minutes") || 30),
            duration_max: Number(formData.get("duration_max") || formData.get("duration_minutes") || 30),
            is_active: formData.get("is_active") === "on",
          }),
        });
        if (!response.ok) {
          toast("Could not save must task.");
          return;
        }
        toast("Must task saved.");
        await loadRoutines();
        return;
      }
      const form = event.target.closest("[data-price-form]");
      if (!form) return;
      event.preventDefault();

      const formData = new FormData(form);
      const response = await fetch("/api/shopping/prices", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          shopping_item_id: form.dataset.priceForm,
          store_name: formData.get("store_name"),
          price: formData.get("price"),
        }),
      });
      if (!response.ok) {
        toast("Could not save price.");
        return;
      }
      toast("Price saved.");
      await loadDashboard();
    });

    document.querySelector("#activity-filters").addEventListener("input", () => {
      window.clearTimeout(window.activityTimer);
      window.activityTimer = window.setTimeout(() => loadActivity(1), 250);
    });
    document.querySelector("#activity-filters").addEventListener("change", () => loadActivity(1));
    document.querySelector("#activity-prev").addEventListener("click", () => loadActivity(Math.max(1, activityPage - 1)));
    document.querySelector("#activity-next").addEventListener("click", () => loadActivity(activityPage + 1));
    document.querySelector("#month-filter").value = currentMonthValue();
    document.querySelector("#start-month-filter").value = currentMonthValue();
    document.querySelector("#end-month-filter").value = currentMonthValue();
    document.querySelector("#year-filter").value = String(new Date().getFullYear());
    const updatePeriodControls = () => {
      const scope = document.querySelector("#scope-filter").value;
      document.querySelector("#month-filter").classList.toggle("hidden", scope !== "month");
      document.querySelector("#range-controls").classList.toggle("open", scope === "range");
      document.querySelector("#year-filter").classList.toggle("hidden", scope !== "year");
    };
    ["scope-filter", "month-filter", "start-month-filter", "end-month-filter", "year-filter"].forEach(id => {
      document.querySelector(`#${id}`).addEventListener("change", () => {
        updatePeriodControls();
        loadDashboard();
      });
    });
    document.querySelector("#day-task-date").value = new Date().toISOString().slice(0, 10);
    document.querySelector("#day-task-date").addEventListener("change", () => {
      if (!document.querySelector("#tasks-view").classList.contains("hidden")) {
        loadDayAgenda();
      }
    });
    document.querySelector("#calendar-day-date").addEventListener("change", () => {
      if (document.querySelector("#calendar-modal").classList.contains("open")) {
        loadCalendarModalAgenda();
      }
    });
    updatePeriodControls();

    loadDashboard().catch(() => {
      document.querySelector("#status").textContent = "Could not load";
      toast("Dashboard data could not be loaded.");
    });
    showCalendarStatusFromUrl();
  </script>
</body>
</html>
"""


def _login_page() -> str:
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Family Copilot Login</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f5f6f8; color: #17181c; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    main { width: min(420px, calc(100% - 32px)); background: white; border: 1px solid #e5e7eb; border-radius: 18px; padding: 24px; box-shadow: 0 16px 40px rgba(15,23,42,.06); }
    h1 { margin: 0 0 10px; font-size: 24px; letter-spacing: 0; }
    p { color: #70737c; line-height: 1.45; margin: 0 0 18px; }
    a { display: inline-flex; align-items: center; justify-content: center; border: 1px solid #d1d5db; border-radius: 999px; padding: 10px 14px; color: #17181c; text-decoration: none; font-weight: 600; }
  </style>
</head>
<body>
  <main>
    <h1>Family Copilot</h1>
    <p>Sign in with the Google account you linked from Telegram. New household members need an invite code and /dashboard_link first.</p>
    <a href="/auth/google/start">Continue with Google</a>
  </main>
</body>
</html>
"""
