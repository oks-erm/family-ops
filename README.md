# Family Copilot

Telegram-first household and routine assistant MVP.

## Stack

- Python 3.12
- FastAPI
- aiogram 3
- PostgreSQL
- SQLAlchemy 2.0 async
- Alembic
- APScheduler
- Docker Compose

## Local Setup

1. Create your environment file:

```bash
cp .env.example .env
```

2. Edit `.env` and set `TELEGRAM_BOT_TOKEN`.

3. Start Postgres and the app:

```bash
docker compose up --build
```

4. Run migrations in another terminal:

```bash
docker compose exec app alembic upgrade head
```

5. Check the API:

```bash
curl http://localhost:8000/health
```

6. Open Telegram and send `/start` to your bot.

## Household and Dashboard Access

Telegram access is invite-based at the household level:

- `/start` creates or updates the Telegram user.
- `/invite` shows the household invite code.
- Another Telegram user joins with `/join CODE`.
- `/dashboard_link` creates a 30-minute link that connects that Telegram user to a Google account.
- `/dashboard` requires Google login and only allows Google emails linked through `/dashboard_link`.

For local Google login, add this redirect URI to the Google OAuth client:

```text
http://localhost:8000/auth/google/callback
```

## Optional pgAdmin

```bash
docker compose --profile devtools up pgadmin
```

pgAdmin runs at `http://localhost:5050`.

## AI Cost Policy

The app is designed to avoid paid AI calls by default:

- Deterministic parsing handles high-confidence commands and is free.
- Light optional AI can use local Ollama or Gemini Flash-Lite for conversational intent routing.
- OpenAI is reserved for heavy tasks, such as receipt extraction or complex planning, and is not used by the current shopping intake.
- The Telegram assistant executes AI output through local services; the model classifies intent or asks a clarification instead of writing directly to the database.

Useful settings:

```env
AI_LIGHT_PROVIDER=deterministic
AI_HEAVY_PROVIDER=openai
OLLAMA_MODEL=llama3.2:3b
GEMINI_MODEL=gemini-3.1-flash-lite
OPENAI_MODEL=gpt-5-mini
```

To run local Ollama through Compose:

```bash
docker compose --profile local-ai up -d ollama
docker compose exec ollama ollama pull llama3.2:3b
```

Then set:

```env
AI_LIGHT_PROVIDER=ollama
```

To use Gemini for light classification instead:

```env
AI_LIGHT_PROVIDER=gemini
GEMINI_API_KEY=your_google_ai_studio_key
GEMINI_MODEL=gemini-3.1-flash-lite
```

## Current MVP Scope

Implemented:

- `/health` FastAPI endpoint
- Telegram `/start` onboarding
- Natural-language Telegram text intake for early assistant behavior
- Shopping phrases like `Need eggs from Lidl`, `Buy rice anywhere`, and `Going to Lidl`
- Shopping cleanup phrases like `Got broccoli` or `Bought milk and eggs`
- Receipt photo extraction through Gemini first, with confirmation before saving or clearing shopping items
- Receipt spend questions like `How much did we spend on groceries this week?`
- Store spend questions like `How much did we spend at Aldi this month?`
- Manual expense/income messages like `petrol €54`, `eat out €43`, or `salary €1500`
- Bank screenshot extraction for expenses and income through Gemini vision
- Minimal `/dashboard` analytics page with duplicate receipt deletion and stored weekly finance/health recommendations
- Dashboard category spend, income/expense chart, shopping price quotes, and Activity Log tab
- Weekly supermarket price/promotion scan for pending shopping items
- Natural task creation with Telegram completion buttons, e.g. `Task: call dentist tomorrow`
- Evening planning prompt, morning daily plan, and evening review scheduled flows
- Per-user daily plans generated from work answers, that user's open tasks, shared shopping, and cached calendar events
- iCal feed storage and sync with `/ical FEED_URL` in Telegram or `POST /calendar/ical`
- Google Calendar OAuth scaffolding through `GET /calendar/google/start`
- Google dashboard login with Telegram-linked household access through `/dashboard_link`
- Mandatory month-end grocery summary scheduled on the last day of each month
- Household-shared shopping lists, receipts, and grocery analytics
- Invite-based household access with `/invite` and `/join CODE`
- Async SQLAlchemy setup
- Alembic schema for users, stores, tasks, task completions, shopping items, daily plans, planning conversations, receipts, calendars, and receipt items

Deferred:

- Google Calendar token refresh
- Receipt correction UI beyond confirm/discard
- HTTPS reverse proxy
- Production backup jobs

## Environment Variables

- `APP_ENV`
- `DATABASE_URL`
- `TELEGRAM_BOT_TOKEN`
- `OPENAI_API_KEY`
- `DEFAULT_TIMEZONE`
- `PUBLIC_BASE_URL`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REDIRECT_URI`
- `DASHBOARD_GOOGLE_REDIRECT_URI`
- `DASHBOARD_SESSION_SECRET`
- `MONTHLY_SUMMARY_TIME`
- `PLANNING_EVENING_TIME`
- `MORNING_PLAN_TIME`
- `EVENING_REVIEW_TIME`
- `WEEKLY_RECOMMENDATION_TIME`

`DATABASE_URL` should use `postgres` as the hostname inside Docker Compose and `localhost` when running the app directly on your machine.

## Deploy Later

The same Docker Compose shape is intended to run on a Hetzner VPS. For production, persist the Postgres volume, add Caddy or Nginx for HTTPS, point Telegram webhooks to `https://domain.com/telegram/webhook`, and add database backups.
