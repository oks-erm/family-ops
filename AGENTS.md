# Family Copilot contributor guide

## Purpose and architecture

Family Copilot is a FastAPI/PostgreSQL household assistant with Telegram, a private web dashboard,
calendar integrations, and public lesson scheduling. Keep HTTP parsing in `app/routes`, workflows in
`app/services`, and persistence in `app/db/repositories`. SQLAlchemy models live in
`app/db/models.py`; every schema change requires an Alembic migration.

Lesson scheduling uses:

- `app/routes/scheduling.py` for the authenticated tutor APIs and unauthenticated booking APIs.
- `app/services/scheduling_service.py` for orchestration and booking safety.
- `app/services/scheduling_rules.py` for deterministic availability calculations.
- `app/services/calendar_service.py` for Google, private iCloud CalDAV, and iCal synchronization.
- `app/services/credential_cipher.py` for encrypted storage of iCloud app-specific passwords.
- PostgreSQL advisory locks to serialize bookings for one tutor.
- `student_meetings` to keep one Google Meet conference per normalized student email and tutor
  profile. Each separately booked lesson copies that conference onto its own calendar event.

## Runtime and commands

Python 3.12 is required. Production runs one Uvicorn worker in Docker Compose so the in-process
APScheduler job executes once.

```bash
docker compose up --build
docker compose exec app alembic upgrade head
docker compose exec app python -m unittest discover -s tests -v
docker compose exec app ruff check app tests
curl http://localhost:8000/health
```

The repository currently contains historical lint debt. For focused changes, report both targeted
lint results and any pre-existing full-repository failures; do not silently reformat unrelated code.

## Configuration and integrations

Configuration comes from environment variables; never commit `.env` or credentials. Important
settings include `DATABASE_URL`, Telegram/AI credentials, Google OAuth credentials,
`PUBLIC_BASE_URL`, `SCHEDULING_PUBLIC_BASE_URL`, `DASHBOARD_SESSION_SECRET`, and
`DEFAULT_TIMEZONE`.

Google OAuth tokens, iCloud app-specific passwords, Google Meet links, conference data, and private
iCal URLs are sensitive. Never log or expose them outside the tutor and the matching student. Apple
credentials must remain Fernet-encrypted at rest; changing `DASHBOARD_SESSION_SECRET` invalidates
stored iCloud credentials and requires reconnection. Calendar-list
access is needed to discover calendars; event access is needed to sync and create lessons. iCal
fetching must remain restricted to resolvable public HTTPS endpoints to prevent SSRF. Calendars that
exist only “On My Mac” cannot be read by the server.

## Safety and correctness

- Keep the tutor management interface authenticated. Public booking endpoints intentionally require
  no login but must validate all inputs and fail closed when calendars cannot be refreshed.
- Preserve the five-minute calendar sync and the immediate pre-booking refresh.
- Keep iCloud CalDAV access read-only, restrict every discovered or redirected URL to HTTPS on an
  `icloud.com` host, and never accept or store the primary Apple Account password.
- Apply commute buffers around non-lesson calendar events only. Confirmed lessons block their
  actual duration and may be booked back-to-back.
- Reuse Google Meet conferences only for the same normalized student email within the same tutor
  profile. Create lesson events with that student as an attendee and send Calendar updates.
- Never weaken OAuth state validation, same-origin checks, URL safety checks, booking locking, or
  overlap checks.
- Treat calendar event titles, student names/emails/notes, tokens, and feed URLs as private data.
- Do not deploy, delete production data, run destructive migrations, send messages, or change DNS
  without explicit user authorization.
- Migrations should be backward-compatible and reversible where practical.

## Deployment and validation

Pushes to `main` build a multi-architecture image, run migrations in the container entrypoint, and
deploy to the existing Hetzner Docker Compose stack. Traefik serves the primary and scheduling
hostnames. After a requested deployment, watch GitHub Actions, verify the expected image, check
`/health`, inspect startup/migration logs, and smoke-test both the private and public scheduling
routes. Confirm DNS and Google OAuth redirect configuration before deploying a new hostname.

For scheduling changes, validate slot boundaries, buffers, notice periods, timezone conversion,
recurrence expansion, cancelled-event cleanup, multi-account calendar selection, concurrent booking
behavior, migration upgrade, and the public booking flow.
