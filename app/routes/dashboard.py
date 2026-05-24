from calendar import monthrange
from datetime import date
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.db.models import ActivityAction, Routine, ShoppingItem
from app.db.repositories.activity import ActivityRepository
from app.db.repositories.finance import FinanceRepository
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.prices import PriceRepository
from app.db.repositories.receipts import ReceiptRepository
from app.db.repositories.routines import RoutineRepository
from app.db.repositories.shopping import ShoppingRepository
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory
from app.services.dashboard_service import DashboardService
from app.services.finance_category_service import FinanceCategoryService
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


class TransactionCategoryRequest(BaseModel):
    category: str


async def _dashboard_context(request: Request, session):
    email = request.session.get("google_email")
    if not email:
        return None, None
    dashboard_user = await UserRepository(session).get_by_google_email(google_email=str(email))
    if dashboard_user is None:
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
    .routine-form {
      margin-bottom: 16px;
      padding-bottom: 16px;
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
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
    }
    .routine-row:last-child { border-bottom: 0; }
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
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .category-select {
      min-width: 128px;
      border: 1px solid #d8dde6;
      background: #fff;
      color: var(--ink);
      border-radius: 999px;
      padding: 7px 28px 7px 11px;
      font-size: 12px;
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
      padding: 20px;
    }
    .modal-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 16px;
    }
    .modal-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 14px;
      margin-bottom: 14px;
    }
    .mini-panel {
      border: 1px solid var(--line);
      border-radius: 16px;
      background: var(--surface-soft);
      padding: 14px;
    }
    .pie-chart {
      width: min(280px, 100%);
      height: auto;
      display: block;
      margin: 0 auto 10px;
    }
    .detail-bars { display: grid; gap: 8px; }
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
    const palette = ["#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#a855f7", "#06b6d4", "#f97316"];
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
      const rows = consolidateRows(rawRows, maxSlices);
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
          <div class="row-sub">${escapeHtml(row.date)}${row.merchant ? ` · ${escapeHtml(row.merchant)}` : ""} · ${escapeHtml(row.source)}</div>
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

      const shoppingItems = flattenShopping(data.shopping);
      setCollapseButton("categories", shoppingItems.length, false);
      document.querySelector("#categories").innerHTML = shoppingItems.length ? `
        <div class="category">
          <div class="category-title">Pending</div>
          <div class="rows">
            ${shoppingItems.map((item, index) => `
              <div class="row ${index >= 6 ? "hidden-row" : ""}" data-collapsible-row="categories">
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
                    <input name="store_name" placeholder="Store" value="${escapeHtml(item.store !== "anywhere" ? item.store : "Lidl")}">
                    <input name="price" inputmode="decimal" placeholder="Price">
                    <button type="submit">Save</button>
                  </form>
                </div>
                <div class="transaction-actions">
                  ${item.price ? `<div class="amount">${escapeHtml(item.price)}</div>` : ""}
                  <button class="delete-btn" type="button" aria-label="Remove shopping item" title="Remove shopping item" data-shopping-delete="${escapeHtml(item.id)}">${iconSvg("delete")}</button>
                </div>
              </div>
            `).join("")}
          </div>
        </div>
      ` : `<div class="empty">No pending shopping items.</div>`;

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
        if (tabButton.dataset.viewTarget === "tasks-view") loadRoutines();
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
    updatePeriodControls();

    loadDashboard().catch(() => {
      document.querySelector("#status").textContent = "Could not load";
      toast("Dashboard data could not be loaded.");
    });
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
