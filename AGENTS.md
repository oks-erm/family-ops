# Family Copilot contributor guide

## Purpose and boundaries

Family Copilot is a Telegram-first household assistant for shopping, tasks, planning,
calendar access and financial records, with a private FastAPI dashboard. PostgreSQL is
its source of truth. Python 3.12 is required; production uses one Uvicorn worker so the
in-process scheduled jobs run once.

**Lesson scheduling is a separate application.** `tutor-scheduling/` is an existing,
untracked local application: do not move, edit, delete or include it in household builds.
Family Copilot must not register scheduling routes, import scheduling workflows, expose
student data to assistant tools, or deploy/restart the scheduling service.

Historical scheduling models/migrations and regression fixtures remain for compatibility
with existing databases. Never drop their tables or rewrite migration history to clean
up the split. `docs/legacy-scheduling.md` is historical reference only.

## Architecture

- `app/routes`: HTTP parsing/authentication and dashboard rendering.
- `app/bot/handlers`: Telegram transport; v2 replies are plain text, never model HTML.
- `app/services/conversation`: bounded conversational orchestration, exact-command routing,
  scoped tool execution, confirmations and generated action receipts.
- `app/schemas/conversation.py`: Pydantic tool contracts; extra arguments are forbidden.
- `app/clients/conversation_model.py`: injectable Responses API adapter. No automatic retries,
  no prompt/body logging, `store=false`, finite timeout/output budget.
- `app/db/repositories`: all new SQL, scoped data access, durable state and token reservations.
- `app/db/models.py` + `alembic/versions`: schema and additive migrations.
- Existing planning/calendar/shopping/finance services remain the domain layer.
- Legacy `AssistantService` remains available while `ASSISTANT_V2_ENABLED=false`.

## Conversation data and safety

`assistant_conversations` stores bounded recent context and one expiring confirmation.
`assistant_turns` deduplicates transport messages; `assistant_actions` records mutation
outcomes. DB mutations, activity entries and action receipts commit together. External
calendar mutations record intent before sending; uncertain outcomes must not be replayed.
Dedicated PostgreSQL advisory locks serialize turns across commits. Token reservations
use row locks, so concurrent users cannot exceed the household allowance.

`assistant_model_calls` contains metadata only: model, route, prompt version, usage, latency,
status and conservative reservations. Unknown provider outcomes retain their reservation.
Old private turn/action content expires on conversation access; IDs and usage are retained
for deduplication/accounting. History is also bounded to eight exchanges. This is not a
background deletion SLA; see `docs/conversation-engine.md` for limitations.

Confirm destructive edits and external calendar changes with an exact expiring code.
A topic change invalidates a pending confirmation. Recheck the selected record/version
before acting. User/household scope is supplied by the application, never model arguments.
Personal tasks stay personal; shopping and finance use existing household membership.
Never let scheduling-only accounts enter the household dashboard or conversation engine.

Finance totals are computed in PostgreSQL with decimal arithmetic, grouped by currency.
Transactions and receipts are separate sources. Preserve the existing dashboard effective-
date convention until the user approves changing it. Flag malformed amounts; never turn
unknown data into a confident zero. Calendar answers disclose cached freshness.

`household_calendar_selections` replaces the household runtime's dependency on tutor
calendar selections. Migration 202609300001 copies household-owned selections, leaving
all tutor records intact. iCloud remains read-only and encrypted; preserve HTTPS/iCloud
host restrictions, public-URL SSRF protections, OAuth state and same-origin protections.

## Configuration

Never commit `.env`, credentials, private URLs or real household conversations.
Existing settings include DATABASE_URL, Telegram/Google/AI credentials,
PUBLIC_BASE_URL, DASHBOARD_SESSION_SECRET and DEFAULT_TIMEZONE.

V2 settings: ASSISTANT_V2_ENABLED (default false), ASSISTANT_MODEL (gpt-6-luna),
ASSISTANT_REASONING_MODEL (gpt-6.1-sol), ASSISTANT_TIMEOUT_SECONDS (30),
ASSISTANT_MAX_OUTPUT_TOKENS (1500), ASSISTANT_MONTHLY_TOKEN_LIMIT (250000 per household),
ASSISTANT_HISTORY_DAYS (7). Zero token allowance disables model calls, not exact commands.
A token limit is not a dollar spending cap. Models/account access must pass live evaluation
before activation. Existing Gemini receipt/bank-image extraction remains unchanged.

## Setup and validation

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt 'ruff>=0.6.9,<1.0.0'
docker compose up --build
# Run only on the intended household database:
docker compose exec app alembic upgrade head
.venv/bin/python -m unittest discover -s tests -v
node tests/test_dashboard_requests.js
.venv/bin/ruff check app tests
```

Database tests require `TEST_DATABASE_URL` explicitly pointing to a migrated disposable
PostgreSQL database whose name ends in `_test`. They create synthetic fixtures and never
connect to the default DB implicitly. CI supplies this variable.

Run targeted conversation tests, then the full suite. Historical lint debt exists; fix new
violations and report remaining pre-existing failures without unrelated formatting.
Model evaluations are opt-in: `python scripts/evaluate_assistant.py --live --model gpt-6-luna`.
This first-response check sends synthetic prompts/tool definitions and executes no tools.
`python scripts/evaluate_conversation_flow.py --live --max-usd 0.10` runs complete turns
with a newly created synthetic household in the explicitly selected local test database.
It never starts Telegram or calendar integrations. Both checks enforce estimated budgets;
confirm authorization for external evaluation payloads and track the total across runs.
Usage reporting: `python scripts/assistant_usage.py --month YYYY-MM` (aggregate-only).

## Deployment

Pushes to `main` verify tests and migrations, build the family image and deploy only
`app` with `--no-deps`. Main compose files contain no scheduling service. The legacy
service definition is preserved separately in `docker-compose.scheduling.yml`; do not
load it in Family Copilot CI. Shared PostgreSQL/Traefik infrastructure still exists;
full infrastructure isolation requires a separately approved operations migration.
Never run `compose down`, `--remove-orphans`, shared proxy restarts, destructive migrations,
or production deployment without explicit authorization. No production changes are
implied by local implementation/testing. After an approved deploy, verify image, health,
migration/startup logs and relevant household smoke tests.

## Change checklist

- Preserve user changes and scheduling separation.
- Validate context switches, corrections, stale/ambiguous IDs and expiring confirmations.
- Test duplicate messages, restart recovery, concurrency, atomic writes and budget exhaustion.
- Test cross-household access, exact totals, currency separation, invalid amounts and pagination.
- Exercise migration upgrade/rollback on disposable data and verify legacy rows are unchanged.
- Check calendar version validation and uncertain external outcomes without live mutations.
- Re-run synthetic model quality/cost checks before enabling a new model or prompt.
- Update these notes when architecture, configuration or operational procedures change.

Production Compose explicitly enables the v2 engine and pins Luna/Sol. Release verification
runs `python scripts/verify_release.py` inside the deployed container without reading household
records or sending messages. Roll back by setting the Compose engine flag to false and
recreating only app; do not roll back additive tables containing conversation data.
The household release owns `/opt/family-copilot/household-release`; use Compose project
`family-copilot` there. Never overwrite the parent directory's Compose or environment files,
which the independent scheduling deployment still uses.
