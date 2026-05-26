# Family Copilot

Family Copilot is a production-focused household operations assistant built around Telegram, with a FastAPI backend, planning engine, shared household workflows, and a web dashboard.

It combines deterministic command routing with optional AI-assisted understanding to keep critical flows predictable while still supporting natural-language interaction.

## Highlights

- Telegram-first assistant for daily operations (tasks, shopping, planning, and finance capture).
- Household collaboration model with invite-based access and shared state.
- Daily planning pipeline with free-window calculation, fixed events, and actionable task scheduling.
- Finance ingestion from text, receipts, and screenshots, with dashboard analytics.
- Calendar integration (iCal and Google OAuth start flow).
- Production deployment via Docker Compose and GitHub Actions.

## Architecture

Core components:

- API and web layer: FastAPI app with dashboard/auth/calendar routes.
- Bot runtime: aiogram-based Telegram bot running in the same service lifecycle.
- Data layer: async SQLAlchemy repositories on PostgreSQL.
- Schema management: Alembic migrations.
- Scheduling layer: APScheduler jobs for daily/weekly/monthly automation.
- AI routing layer: deterministic-first intent handling with pluggable providers for light/heavy tasks.

## Tech Stack

- Python 3.12
- FastAPI
- aiogram 3
- PostgreSQL
- SQLAlchemy 2.0 (async)
- Alembic
- APScheduler
- Docker Compose

## Repository Layout

- `app/bot` Telegram handlers and keyboards
- `app/routes` FastAPI routes for dashboard, auth, and calendar
- `app/services` assistant, planning, shopping, finance, scheduler, and integrations
- `app/db/repositories` data access layer
- `alembic` migration history

## Local Development

1. Create environment file from template.

```bash
cp .env.example .env
```

2. Set required secrets in `.env` (at minimum `TELEGRAM_BOT_TOKEN`).

3. Start services.

```bash
docker compose up --build
```

4. Run database migrations.

```bash
docker compose exec app alembic upgrade head
```

5. Verify service health.

```bash
curl http://localhost:8000/health
```

6. Open Telegram and send `/start` to your bot.

## Authentication and Access Model

- Telegram identity is created/updated via `/start`.
- Household membership is invite-based using `/invite` and `/join CODE`.
- Dashboard access is tied to Telegram identity through `/dashboard_link` and Google login.
- The dashboard only authorizes Google accounts linked through the Telegram flow.

For local OAuth testing, ensure your Google OAuth client includes:

```text
http://localhost:8000/auth/google/callback
```

## Dashboard Overview

The web dashboard is available at `/dashboard` after Google sign-in through the Telegram linking flow.

Current dashboard capabilities include:

- Daily agenda panel with must tasks, day tasks, and events.
- Task actions (add, complete/skip, delete, and move when allowed).
- Event actions for day-level planning events (delete/move for note-based events).
- Planning defaults management (work window, wake/sleep times, commute, meal assumptions).
- Finance and receipt analytics views.

## Telegram Commands

Core bot commands:

- `/start` Create or refresh the Telegram user profile.
- `/invite` Generate or view the household invite code.
- `/join CODE` Join an existing household using an invite code.
- `/dashboard_link` Generate a short-lived link to connect Google login for dashboard access.
- `/ical URL` Attach an iCal feed URL for calendar sync.

Everything else is natural-language driven in regular messages (for example tasks, planning, shopping, and finance capture).

## AI Provider Strategy

Default behavior is deterministic-first to reduce cost and improve predictability.

- `AI_LIGHT_PROVIDER=deterministic` for high-confidence command handling.
- Optional light providers: `ollama` or `gemini` for classification/routing.
- Heavy provider (default `openai`) reserved for higher-complexity tasks.

Example provider settings:

```env
AI_LIGHT_PROVIDER=deterministic
AI_HEAVY_PROVIDER=openai
OPENAI_MODEL=gpt-5-mini
GEMINI_MODEL=gemini-3.1-flash-lite
OLLAMA_MODEL=llama3.2:3b
```

Optional local Ollama profile:

```bash
docker compose --profile local-ai up -d ollama
docker compose exec ollama ollama pull llama3.2:3b
```

## Configuration

Refer to `.env.example` for the complete variable list.

Common required variables:

- `APP_ENV`
- `DATABASE_URL`
- `TELEGRAM_BOT_TOKEN`
- `DEFAULT_TIMEZONE`
- `PUBLIC_BASE_URL`
- `DASHBOARD_SESSION_SECRET`
- Google OAuth settings (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, redirect URIs)

Database hostname guidance:

- Inside Docker Compose: use `postgres`.
- Running app directly on host: use `localhost`.

## Security and Privacy

This repository intentionally avoids hardcoding credentials.

- Never commit `.env`, API keys, bot tokens, OAuth client secrets, or private keys.
- Use GitHub Secrets (or equivalent secret manager) for CI/CD and production values.
- Keep example values in `.env.example` non-sensitive and local-safe.
- Rotate any credential immediately if it appears in logs, screenshots, or chat transcripts.

## Deployment

Production deployment uses:

- GitHub Actions workflow for build and deploy.
- GHCR image publishing.
- Docker Compose runtime on a VPS.

Typical flow:

1. Push to `main`.
2. CI builds and pushes image tags.
3. Deploy job updates server `.env` image tag and restarts stack.

## Roadmap

- Google Calendar token refresh lifecycle
- Extended receipt correction UX
- Hardened backup/restore runbooks
- Additional observability and operational dashboards
