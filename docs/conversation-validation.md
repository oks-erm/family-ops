# Conversation overhaul validation — 2026-09-30

Branch: `codex/conversation-core`. Live provider validation passed.
The user subsequently authorized push and production deployment; production Compose enables
Luna with the Sol fallback. Deployment status is tracked in GitHub Actions.

## Completed checks

- Full Python suite: **132 tests passed**, including **15 PostgreSQL integration tests**.
- Exact-command routing: model is not called.
- Persisted follow-up context, planning interruptions, escalation and bounded tool loops.
- Duplicate messages across fresh service sessions, atomic action receipts and simultaneous turns.
- Concurrent household budget reservation and actual-usage settlement.
- Cross-household access prevention, personal task scope and cleared context after household changes.
- Confirmation expiry, exact codes, invalidation on topic changes and changed-record rejection.
- Exact finance totals beyond the old 300-record limit, separate currencies, pagination and malformed amounts.
- Planning notes append by default; replacement is explicit.
- Group messages cannot overwrite private reminder destinations; model replies use plain text.
- Calendar edits reject stale ETags and target exact records; no live calendar changes were performed.
- New migration: fresh upgrade, seeded legacy-selection copy, downgrade and re-upgrade passed in disposable databases.
  The synthetic legacy scheduling row remained intact throughout; copied selection flags matched.
- Health smoke check returned 200. Lesson routes returned 404 from the household app.
- Dashboard JavaScript syntax and out-of-order/network-failure tests passed.
- Compose/workflow YAML parsing, Python compilation and `git diff --check` passed.
- Targeted lint for all new conversation/schema/client/repository/test/evaluation modules passed.
- Full-repository lint: **523 findings**, versus **524** at the unchanged starting commit.
  Remaining findings are historical debt, primarily long lines. Unrelated files were not reformatted.

## Live provider validation

The user approved exporting tool definitions and synthetic conversations with a $0.50
estimated total limit. No real household records were sent. The HTTP 400 was caused by
Pydantic's generated Decimal regex using lookaround unsupported by the provider. An explicit
money-string schema fixes API compatibility; independent Decimal precision and range checks
remain enforced and have a regression test.

| Check | Result | Estimated USD, including cache writes |
| --- | --- | ---: |
| Luna: six first-response cases | 6/6 passed | 0.00061180 |
| Astra: same six cases | 6/6 passed | 0.04558000 |
| First complete-flow run | 9/10 scored; valid euro symbol rejected by evaluator | 0.00114561 |
| Fresh complete-flow repeat, corrected currency scoring | 10/10 passed | 0.00108692 |
| Astra stateless tool-result continuation | Passed; correctly reported empty list | 0.00271550 |
| Sol: same six cases, replacement fallback | 6/6 passed | 0.00755100 |
| **Total successful requests** | **Below approved $0.50 limit** | **0.05869083** |

Rates: [OpenAI standard short-context pricing](https://developers.openai.com/api/docs/pricing),
verified September 30, 2026. Cache-write premiums are included. The rejected diagnostic
returned no usage; these estimates are not a provider invoice.

The six cases cover typos, interrupted planning/topic changes, a financial date follow-up,
ambiguous references, task vs shopping intent, and income capture. Median first-response
latency was 1.981 seconds for Luna and 2.809 seconds for Astra. This small smoke set supports
using Luna for these routine cases; it does not establish broad comparative model quality
or a production cost-saving percentage.

Complete-flow checks used actual models, the conversation service and PostgreSQL, with a
new synthetic household and a fresh service/session for each message. They verified two-item
creation, a correction, switching to a dated task and back to shopping, removal proposals,
exact-code confirmation, deterministic reads, duplicate-message receipts after a new session,
and recording/querying income. Confirmation, exact reads and duplicates used zero model calls.
No Telegram sender or calendar integration was started.

Evidence is retained in `artifacts/assistant-*-evaluation.json`,
`artifacts/assistant-astra-continuation.json` and `artifacts/assistant-validation-summary.json`.
The initial scoring failure is retained for transparency; the corrected repeat passed.

## Remaining limitations

Synthetic smoke tests do not prove reliability across all real conversations. Broader sanitized
failure examples are still needed. Local unit/integration tests independently cover application
safety and persistence. Production activation is separately verified by the release workflow.

Remote CI/CD, production database configuration, container build and live Telegram behavior
were not verified. Shared PostgreSQL/Traefik infrastructure remains an operational dependency;
its physical/credential isolation needs a separately reviewed production migration. The
standalone scheduling application was not edited. These validation results were recorded before the authorized release.

## Cheaper default routing

At the user's request, GPT-6.1 Sol replaces Astra as the default reasoning fallback;
GPT-6 Luna remains the everyday model. The updated escalation test and all 23 focused
conversation/evaluation tests passed, as did targeted lint. Sol passed all six live
first-response fixtures. The production Compose release configuration now enables these models.

Average model-call estimates on the same six-case set: Luna $0.000102, Sol $0.001259,
Astra $0.007597. Luna's complete-flow repeat used 14 calls for seven AI-assisted requests
($0.000155 per assisted request); three additional requests needed no model. These are
small synthetic-sample measurements with cache reuse, not fixed per-request prices.

## Release checks

The release workflow verifies the exact image tag, migration revision, conversation flag,
model selection, configured credentials (presence only), database tables, internal/public health,
unauthenticated dashboard rejection, and absent household lesson routes. It checks startup
logs for errors without publishing private payloads, and verifies pre-existing non-app containers
retain their IDs and remain running. It does not prune images or restart shared infrastructure.

## Multi-household release v2.2 — 2026-09-30

- **161 automated tests passed**, including 32 PostgreSQL integration tests. New coverage
  includes durable ingestion, per-chat ordering, household fairness, global concurrency,
  stale ownership, crash recovery, uncertain delivery, safe rate-limit retries and membership
  changes. A pool of two connections supported twenty simultaneous conversation leases.
- Luna passed **24/24** first-response routing/wording cases and **14/14** complete-flow
  steps. The flow used 16 Luna calls; simple shopping/task/income writes each needed one.
  Confirmation, exact reads and duplicate receipts needed zero. No Sol escalation was needed
  in this flow; that does not establish comparative model quality.
- Additional live evaluation estimates: **$0.002204 + $0.00198340 = $0.00418740**.
  Cumulative successful-call estimate across both releases: **$0.06287823**, below the
  approved **$0.50** total. Synthetic prompts only; no live messages or calendar mutations.
- Local queue benchmark: **100 households, 300 messages, 12 worker loops, 2 DB connections**;
  peak active work **8**, matching the configured global cap. All channels remained ordered.
  It took **4.189 seconds** (71.61 messages/second); claim/work/finish p50 **112.4ms**,
  p95 **222.4ms**, with simulated 20ms work. This is queue throughput, not production
  capacity, end-to-end model latency, or a scale guarantee.
- Migration 202609300002 passed fresh upgrade, downgrade/re-upgrade, seeded planning
  preservation and conservative historical cost backfill. Token totals remained unchanged.
  Plans are now unique per user/household/date, preventing transfers by same-day upserts.
- Targeted lint passed; full app/tests lint retains **515 historical findings** (previous
  release: 523). Compilation, dashboard request tests, configuration parsing and diff checks
  passed. Web lifespan starts neither Telegram nor scheduled jobs.
- Artifacts: `assistant-luna-routing-v22.json`, `assistant-flow-v22.json`,
  `assistant-queue-benchmark.json`, and the cumulative validation summary.

Dollar caps cover v2 conversation calls, not legacy image extraction. Legacy commands/images
and periodic notifications retain direct sends; uncertain legacy jobs are not blindly replayed.
Queue backlog and retained metadata require monitoring. Physical infrastructure and credentials
remain shared with the separate scheduling deployment. This release does not claim universal
exactly-once delivery or unlimited household capacity.


## Screenshot regressions and image preservation — v2.3

- **169 automated tests passed**. New database/worker tests reproduce confirmation copy
  help followed by a button click, duplicate callbacks, whole-October weekday hours, preserved
  notes, cross-household purchase isolation, receipt preview/confirmation and bank-file import.
- Asking for a copyable confirmation no longer retires the pending change. Original expiry
  and record-version checks still apply; Confirm/Cancel buttons carry the exact scoped code.
- Month-long work hours use one atomic range operation; October 2026 saves all 22 weekdays.
  Receipt-item frequency is computed in SQL and suggestions do not automatically add items.
- Luna final routing passed **27/27**. Complete-flow repeat and final run passed **18/18**, including
  month follow-up and purchase suggestions. Initial failures remain in the artifacts: current
  groceries were confused with past purchases, time schemas produced UTC offsets, and an
  ungrounded reference prompted a list read. These were corrected in routing instructions
  and the local-time schema. A separate case-sensitive scoring error was also corrected.
- Two live **Gemini 3.1 Flash-Lite** calls extracted a generated receipt and bank screenshot
  correctly. Both returned HTTP 200; estimated cost **$0.000973**. No real user images,
  household data or Telegram messages were sent. Synthetic worker tests independently
  exercise receipt confirmation, plain-text previews and PNG document MIME handling.
- Targeted lint, Python compilation, dashboard request tests and diff checks passed. Full
  lint retains **510 historical findings** (previous release: 515). No dependencies, schema
  migrations, dashboard layouts or lesson-scheduling code changed.
- Cumulative live validation estimate: **$0.07976786**, below the approved **$0.50**.
  Per-run costs are in `artifacts/assistant-validation-summary.json`.
  Image validation uses Google pricing; all other runs use the recorded OpenAI pricing.

Image extraction still uses its existing provider and is outside the v2 conversation budget
controls. New photos are processed in memory; confirmation uses persisted extraction.
Purchase suggestions cover saved receipts and exact case/space-normalized item names, not
all spending or inferred inventory. These synthetic checks are regression evidence, not a
guarantee for every receipt or wording. Live Telegram delivery is not exercised by tests.
