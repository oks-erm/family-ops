# Household conversation engine v2

Local defaults retain `ASSISTANT_V2_ENABLED=false`; the authorized production Compose release
enables the engine with Luna and Sol explicitly. It uses one bounded
orchestrator and existing PostgreSQL/service infrastructure. Lesson scheduling is excluded.

## Execution

Telegram private message -> persisted conversation -> exact command or Responses model ->
validated tool -> scoped repository/service -> action receipt -> persisted response.

Exact commands (`shopping list`, `show my tasks`, `shopping: milk`, `task: call dentist`)
need no model. Natural variations use GPT-6 Luna. Difficult reasoning can escalate
once to GPT-6.1 Sol, as can repeated invalid tool attempts before any successful
write. A five-round limit and four-call-per-response limit bound requests. The application
validates every argument; model confidence never grants permissions. Subsequent models
receive prior outcomes rather than replaying the workflow.

Astra is excluded from the default routing; its earlier evaluation is retained as comparison
evidence. Both model names remain configurable through the existing environment settings.

Models see recent conversation plus a compact snapshot of prior tool evidence and unfinished
planning context. Planning questions do not intercept unrelated topics. Current facts must
be re-read. Each tool returns source IDs; edits require an ID the conversation has retrieved.

Tools cover shopping/tasks, transaction recording, exact financial queries, daily planning,
work hours/notes, and confirmed household Google Calendar changes. Calendar answers read
the existing synchronized cache. iCloud/iCal are read-only. Existing receipt-image processing
continues through its existing handler; multimodal conversation history is not yet unified.

## Financial semantics

Default spending/income queries use financial transactions. Receipt questions use receipt
headers, or matching receipt items when an item search is supplied. Do not add sources.
Amounts are aggregated in PostgreSQL, grouped by currency, over all matching rows. Details
are paginated separately. Invalid amount strings produce an incomplete-result flag.

Transaction dates preserve the existing dashboard rule: use recorded dates in the import
month, otherwise the import date. This rule is surprising for late imports but changing it
would alter historical reports, so it needs a separate decision. The returned evidence names
this convention. Receipt dates use purchase date, falling back to import date. Category
normalization currently includes transport/commute and exact category names.

## Durability, privacy and limits

A turn is identified by user, channel and Telegram message ID. Duplicates return the saved
response. Started-but-unfinished turns never replay writes automatically. Household changes
clear conversation context. Database mutations and action receipts commit atomically; an
external action's started marker is committed first. An uncertain external outcome requires
checking the provider before a new request. Calendar edits use cached/provider ETags.

Removal and calendar proposals require `confirm CODE`, expire after ten minutes, and are
invalidated by a new topic. A bare yes/number is not authorization. The selected record is
checked again before a confirmed action. Calendar attendee emails and communications are
not exposed as assistant tool parameters.

History is bounded to eight exchanges and expires after the configured age on next access.
Private old turn/action contents are scrubbed on access; deduplication IDs and usage metadata
remain. Dormant conversations are not periodically purged yet. Provider calls request
`store=false`; normal provider account data policies still apply.

The default monthly budget is 250,000 reserved/actual tokens per household, not dollars.
Input reservation uses UTF-8 payload bytes plus output allowance and overhead. Success
settles actual usage; unknown failures retain their reservation. Caps are conservative,
may reject a large request before the apparent remaining allowance is spent, and reset by
UTC month. Cost reports are estimates using explicitly dated rates; provider invoices are
final. Usage reports show a range because stored metadata does not separate cache writes;
the upper estimate includes their premium. Live evaluation artifacts use the reported cache
writes for a precise token-based estimate. No household content is included in usage reports.

## Validation and activation

1. Apply additive migrations to a disposable database, run all unit/database tests and the
   dashboard JavaScript test. Verify the copied household calendar selections and unchanged
   legacy scheduling records.
2. Run the opt-in synthetic evaluation using the intended API account. A rejected API call
   is not a quality result. Inspect tool selection, argument correctness, latency and usage.
3. Compare everyday/strong model results on the same fixtures. Extend them with sanitized
   examples of actual failures; the initial six examples are smoke tests, not a statistical
   quality guarantee.
4. Agree the household token allowance/model choices and retention requirements. Enable
   `ASSISTANT_V2_ENABLED=true` only after provider verification and an approved deployment.
5. Check `/health`, application logs and synthetic household flows. Roll back the engine
   by setting the production Compose flag to `"false"` and recreating only `app`; no
   destructive schema rollback is needed.

Example checks:

```sh
python -m unittest discover -s tests -p 'test_conversation*.py' -v
TEST_DATABASE_URL=postgresql+asyncpg://.../family_copilot_test python -m unittest discover -s tests -v
node tests/test_dashboard_requests.js
python scripts/evaluate_assistant.py --live --model gpt-6-luna --max-usd 0.50
TEST_DATABASE_URL=postgresql+asyncpg://.../family_copilot_test python scripts/evaluate_conversation_flow.py --live --max-usd 0.10
python scripts/assistant_usage.py --month 2026-09
```

## Scheduling boundary

Main compose files and image exclude the independent scheduling app. Family CI updates
only the app service without dependencies or proxy restarts. Historical scheduling models,
migrations and tests are retained to preserve existing database history, not exposed through
assistant tools. The standalone scheduling app was not changed. PostgreSQL and Traefik can
still share a host with scheduling; separate credentials/roles, infrastructure and service
ownership require a reviewed production operations change. Never use `down` or
`--remove-orphans` as part of the household release.

## Dashboard

The current UI is retained. Dashboard/activity loads now discard stale responses, handle
network failures, and keep the prior view usable on a failed refresh. A UI redesign and
adding web chat are outside this change.
