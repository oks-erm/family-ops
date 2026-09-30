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
