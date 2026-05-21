from datetime import date
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from fastapi.responses import HTMLResponse

from app.db.models import ActivityAction, ShoppingItem
from app.db.repositories.activity import ActivityRepository
from app.db.repositories.households import HouseholdRepository
from app.db.repositories.prices import PriceRepository
from app.db.repositories.receipts import ReceiptRepository
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory
from app.services.dashboard_service import DashboardService
from app.utils.datetime import now_in_timezone

router = APIRouter()


class ManualPriceRequest(BaseModel):
    shopping_item_id: UUID
    store_name: str
    price: str
    product_name: str | None = None


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
async def dashboard_data(request: Request) -> dict[str, object]:
    async with async_session_factory() as session:
        dashboard_user, household = await _dashboard_context(request, session)
        if dashboard_user is None or household is None:
            raise HTTPException(status_code=401, detail="Dashboard login required.")
        today = now_in_timezone(dashboard_user.timezone).date()
        return await DashboardService(session).summary(household_id=household.id, today=today)


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
    .grid { display: grid; gap: 14px; }
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
    .bar-row {
      display: grid;
      grid-template-columns: minmax(90px, 150px) minmax(120px, 1fr) 88px;
      gap: 12px;
      align-items: center;
      min-height: 38px;
    }
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
    @media (max-width: 900px) {
      .metrics, .main-grid { grid-template-columns: 1fr; }
      .topbar { align-items: start; flex-direction: column; }
      .brand { align-items: start; flex-direction: column; gap: 12px; }
      .status { white-space: normal; }
    }
    @media (max-width: 620px) {
      .shell { width: min(100% - 22px, 1180px); padding-top: 18px; }
      h1 { font-size: 23px; }
      .panel, .metric { border-radius: 16px; }
      .bar-row { grid-template-columns: 1fr; gap: 7px; padding: 8px 0; }
      .filters { grid-template-columns: 1fr; }
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
          <button class="tab-btn" type="button" data-view-target="activity-view">Activity Log</button>
        </nav>
      </div>
      <div class="status" id="status">Loading dashboard</div>
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
            <h2>Income By Category This Month</h2>
          </div>
          <div id="income-categories"></div>
        </section>

        <section class="panel">
          <div class="section-head">
            <h2>Shopping List</h2>
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
            <h2>Recent Transactions</h2>
          </div>
          <div class="rows" id="transactions"></div>
        </section>

        <section class="panel">
          <div class="section-head">
            <h2>Recommendations</h2>
          </div>
          <div class="rows" id="recommendations"></div>
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
    const flattenShopping = groups => groups.flatMap(group => group.items.map(item => ({
      ...item,
      category: group.category,
    })));
    const iconSvg = name => {
      if (name === "edit") {
        return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5Z"/></svg>`;
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
    const renderBars = (rows, emptyText) => {
      const max = Math.max(...rows.map(x => moneyNumber(x.value)), 1);
      return rows.length ? rows.map(row => {
        const width = Math.max(4, moneyNumber(row.value) / max * 100);
        return `
          <div class="bar-row">
            <div class="row-title">${escapeHtml(row.label)}</div>
            <div class="bar-track"><div class="bar" style="width:${width}%"></div></div>
            <div class="amount">${escapeHtml(row.value)}</div>
          </div>
        `;
      }).join("") : `<div class="empty">${escapeHtml(emptyText)}</div>`;
    };
    const toast = message => {
      const el = document.querySelector("#toast");
      el.textContent = message;
      el.classList.add("show");
      window.clearTimeout(window.toastTimer);
      window.toastTimer = window.setTimeout(() => el.classList.remove("show"), 2400);
    };

    async function loadDashboard() {
      document.querySelector("#status").textContent = "Updating";
      const response = await fetch("/api/dashboard");
      const data = await response.json();
      if (data.error) {
        document.querySelector(".shell").innerHTML = `<div class="panel">${escapeHtml(data.error)}</div>`;
        return;
      }

      const t = data.totals;
      document.querySelector("#metrics").innerHTML = [
        metric("Expenses Week", t.this_week),
        metric("Expenses Month", t.this_month),
        metric("Income Month", t.income_month),
        metric("Next Month", t.next_month_projection),
      ].join("");

      document.querySelector("#expense-categories").innerHTML = renderBars(data.expense_categories, "No expenses logged this month.");
      document.querySelector("#income-categories").innerHTML = renderBars(data.income_categories || [], "No income logged this month.");

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
                ${item.price ? `<div class="amount">${escapeHtml(item.price)}</div>` : ""}
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

      document.querySelector("#transactions").innerHTML = data.transactions.length ? data.transactions.map(item => `
        <div class="row">
          <div class="row-main">
            <div class="row-title">${escapeHtml(item.description)}</div>
            <div class="row-sub">${escapeHtml(item.date)} · ${escapeHtml(item.type)} · ${escapeHtml(item.category)}${item.merchant ? ` · ${escapeHtml(item.merchant)}` : ""}</div>
          </div>
          <div class="amount">${item.type === "income" ? "+" : "-"}${escapeHtml(item.amount)}</div>
        </div>
      `).join("") : `<div class="empty">No transactions logged this month.</div>`;

      document.querySelector("#activity").innerHTML = data.activity.length ? data.activity.map(item => `
        <div class="row">
          <div class="row-main">
            <div class="row-title">${escapeHtml(item.summary)}</div>
            <div class="row-sub">${escapeHtml(item.actor)} · ${escapeHtml(item.entity_type)} · ${escapeHtml(item.action)}${item.category ? ` · ${escapeHtml(item.category)}` : ""}</div>
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
            <div class="row-sub">${escapeHtml(item.actor)} · ${escapeHtml(item.entity_type)} · ${escapeHtml(item.action)}${item.category ? ` · ${escapeHtml(item.category)}` : ""} · ${escapeHtml(new Date(item.date).toLocaleString())}</div>
          </div>
        </div>
      `).join("") : `<div class="empty">No activity matches these filters.</div>`;
      document.querySelector("#activity-page").textContent = `Page ${data.page} of ${data.pages} · ${data.total} results`;
      document.querySelector("#activity-prev").disabled = data.page <= 1;
      document.querySelector("#activity-next").disabled = data.page >= data.pages;
    }

    document.addEventListener("click", async event => {
      const tabButton = event.target.closest("[data-view-target]");
      if (tabButton) {
        document.querySelectorAll(".tab-btn").forEach(button => button.classList.remove("active"));
        tabButton.classList.add("active");
        document.querySelectorAll(".view").forEach(view => view.classList.add("hidden"));
        document.querySelector(`#${tabButton.dataset.viewTarget}`).classList.remove("hidden");
        if (tabButton.dataset.viewTarget === "activity-view") loadActivity(1);
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

    document.addEventListener("submit", async event => {
      if (event.target.id === "activity-filters") {
        event.preventDefault();
        await loadActivity(1);
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
