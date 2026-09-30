# Household conversation engine v2

Local defaults retain `ASSISTANT_V2_ENABLED=false`; the authorized production Compose release
enables the engine with Luna and Sol explicitly. It uses one bounded
orchestrator and existing PostgreSQL/service infrastructure. Lesson scheduling is excluded.

## Execution

Telegram ingress -> durable inbox -> ordered worker -> persisted conversation -> exact command
or Responses model -> validated tool -> scoped repository/service -> action receipt -> durable
outbox -> Telegram delivery. Web requests and scheduled jobs run in separate processes.

Final model answers have validated reply/topic/clarification fields. Bounded per-topic references
help return to an earlier subject. Topic state expires under ASSISTANT_HISTORY_DAYS. A write
marked as fulfilling the whole request returns its authoritative receipt without another model
call; compound requests continue until their remaining actions or questions are handled.

Exact commands (`shopping list`, `show my tasks`, `shopping: milk`, `task: call dentist`)
need no model. Natural variations use GPT-6 Luna. Difficult reasoning can escalate
once to GPT-6.1 Sol for difficult reasoning or an incomplete response. Missing facts and
ambiguous references prompt clarification; repeated invalid tool calls stop safely. A five-round limit and four-call-per-response limit bound requests. The application
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

The default monthly budget remains 250,000 reserved/actual tokens per household. Optional
per-household dollar caps default to unset. `scripts/assistant_limits.py` previews limits and
requires `--apply` to save them; it refuses a dollar cap if current-month calls cannot be priced.
Unknown models cannot run under an active dollar cap. These caps cover the conversation
engine; existing image extraction/legacy AI providers are not included.
Input reservation uses UTF-8 payload bytes plus output allowance and overhead. Success
settles actual usage; unknown failures retain their reservation. Caps are conservative,
may reject a large request before the apparent remaining allowance is spent, and reset by
UTC month. Cost reports are estimates using explicitly dated rates; provider invoices are
final. Usage reports now include cache writes and recorded estimated dollars. Older calls are
backfilled conservatively with cache-write premiums; unknown outcomes retain reservations. Live evaluation artifacts use the reported cache
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
5. Check `/health`, all runtime health checks, image tags, startup logs and synthetic flows.
   Disable the engine by setting the Compose flag to `"false"` and recreating ingress/worker.
   An image rollback to the old monolith must stop ingress/worker/delivery/scheduler first.
   Keep additive tables; do not downgrade production queue data.

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
only the five household services without dependencies or proxy restarts. Historical scheduling models,
migrations and tests are retained to preserve existing database history, not exposed through
assistant tools. The standalone scheduling app was not changed. PostgreSQL and Traefik can
still share a host with scheduling; separate credentials/roles, infrastructure and service
ownership require a reviewed production operations change. Never use `down` or
`--remove-orphans` as part of the household release.

Household deployments write Compose and environment files under
`/opt/family-copilot/household-release`, retaining the `family-copilot` Compose project
for its existing network and app service. The parent directory's scheduling Compose file,
environment and pinned scheduling image are left intact for its independent release workflow.

## Dashboard

The current UI is retained. Dashboard/activity loads now discard stale responses, handle
network failures, and keep the prior view usable on a failed refresh. A UI redesign and
adding web chat are outside this change.


## Multi-household operation

`app.runtime` provides ingress, worker, delivery and scheduler roles. Ingress acknowledges
Telegram's offset only after durable insertion. One leased ingress and one leased scheduler
run at a time; workers and delivery can have replicas. Each message's channel is sequential.
Different households share bounded capacity fairly (least recently served first). Default limits
are 12 active jobs globally, 2 per household and 30 starts per minute per household. Each worker
process runs 4 loops, so the initial deployment has at most 4 active jobs. Queued messages are
rebound to current membership after a join. Existing records never move implicitly.

Conversation/job leases renew with short database transactions. Ownership checks fence writes
and completion; model waits do not reserve a DB connection. Expired safe jobs recover a saved
receipt or report an unfinished turn; they do not replay uncertain mutations. Legacy updates
are not replayed after an uncertain crash. Turn execution has a 180-second deadline.

Delivery has separate ordering and leases. Telegram's explicit rate-limit rejection can retry;
a timeout or expired send becomes uncertain because Telegram offers no idempotent send key.
Further parts of that reply are suppressed. `/last_reply` safely retrieves the saved response.
Successful inbox payloads and delivered bodies are cleared; failed payloads expire after the
history period through hourly maintenance. Metadata remains for deduplication and diagnostics.
Pending queue depth is not capped; monitor it and provision capacity before admitting a large
influx. Periodic job fan-out and legacy image/command sends are not a universal exactly-once
pipeline. This release establishes worker isolation, not unlimited production capacity.

Scale workers with `docker compose ... up -d --no-deps --scale worker=2 worker` only after
checking database connection capacity. Each process has a 5+5 pool; web replicas also multiply
pools. The global queue limit still applies across replicas. Keep the scheduler singleton and
retain shared infrastructure ownership. `scripts/assistant_usage.py` reports aggregate model
usage and queue states without household messages or identifiers. The queue benchmark is
local, synthetic and contains no live sends or model calls.

Daily plans and planning conversations are unique per user/household/date. Existing records
are preserved; the constraint names remain stable for rollout compatibility. Downgrade fails
if multiple household plans exist for the same person/date rather than discarding records.
After such records exist, use a forward fix or the new runtime with the engine disabled;
older readers assumed only one plan per person/date and are not a safe image rollback.
