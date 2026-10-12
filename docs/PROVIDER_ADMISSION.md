# Shared provider admission: Phase 3 increment

## Implemented

LLM admission keeps the existing PostgreSQL transaction-scoped advisory lock and row lock. The transaction now reserves a request and an **estimated token allowance** atomically before execution. Failover and repair attempts each reserve independently. Provider calls remain outside database transactions.

An optional holdback prevents background graph extraction from using the last configured request/token allowance in a UTC minute or day. Interactive answers and practice exams can use the full budget but cannot exceed its total cap. This is capacity reservation, not a preemptive scheduler or an answer-latency guarantee. Unused held-back capacity may reduce background throughput.

Application-budget denials no longer open the vendor circuit breaker. Otherwise a graph-only holdback would inadvertently block the next interactive answer despite its reserved capacity. Real vendor quota/timeouts retain their existing cooldown behavior, and an application denial does not clear an existing vendor cooldown.

Reservations are retained after failure or cancellation following admission, because the server may already have consumed provider capacity. A rejected/cancelled admission that never commits does not consume a reservation. If several windows are full, the returned retry delay waits for the longest applicable reset.

## Configuration and defaults

Existing `SHARED_PROVIDER_LIMITS_ENABLED`, scope, minute/day request limits are unchanged. New enforcement is **opt-in**, with all six settings defaulting to zero. No environment file or Render setting was changed in this increment.

| Setting | Meaning |
| --- | --- |
| `PROVIDER_ESTIMATED_TOKENS_PER_MINUTE` | Total estimated-token admission cap per provider/minute; zero disables it |
| `PROVIDER_ESTIMATED_TOKENS_PER_DAY` | Total estimated-token admission cap per provider/UTC day; zero disables it |
| `PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_MINUTE` | Requests background work cannot spend in a minute |
| `PROVIDER_INTERACTIVE_REQUEST_RESERVE_PER_DAY` | Requests background work cannot spend in a day |
| `PROVIDER_INTERACTIVE_TOKEN_RESERVE_PER_MINUTE` | Estimated-token holdback from background work in a minute |
| `PROVIDER_INTERACTIVE_TOKEN_RESERVE_PER_DAY` | Estimated-token holdback from background work in a day |

Reserves must be smaller than the corresponding enabled limit. API and workers sharing a scope must use the same limits. Do not enable token enforcement while an older process still writes request-only reservations; wait for an accounting window reset after upgrading all processes. Calls predating the upgrade cannot be reconstructed from request counts.

`/api/v1/providers/status` exposes configured limits, holdbacks and current-window estimates beside request counts. New response fields are additive. They are not vendor usage, remaining free credit or a billing guarantee.

## Estimation limitations

The current reservation is `ceil(input_characters / 4)` plus an operation allowance: graph extraction 2,000, answer synthesis 1,200, exam generation 2,500. The allowance stays reserved even when less output was generated. Unknown operations have no assumed output allowance and must supply a deliberate estimate when integrating a new provider boundary.

This is approximate—not a tokenizer or a worst-case bound. Language, formatting, system-prompt accounting and model reasoning affect real usage. Groq answer/exam generation does not currently enforce these output allowances as hard completion limits. SDK response token usage is not yet persisted. Therefore these budgets cannot promise prevention of vendor token-limit errors or charges; existing request limits and vendor protections still apply.

Hosted Qdrant embedding and Cohere reranking are **not yet integrated** into this shared admission path. They need separate operation/provider boundaries and accounting. The comparison reranker performs two requests, which must eventually be counted individually. Durable delayed graph scheduling, versioned cross-upload caching and real API/ingestion-worker validation remain unfinished Phase 3 work.

## Migration and rollback

The existing startup schema lock protects addition of two `BIGINT NOT NULL DEFAULT 0` columns to `provider_budgets`. The migration is additive and idempotent; existing request counts are preserved. The previous release's column-named INSERT/UPDATE remains valid. Rolling application code back does not require dropping columns or deleting usage. Leave new enforcement disabled during mixed-version operation.

Local tests initialized the previous six-column table, ran concurrent full schema initialization twice, checked preserved counters/defaults and exercised previous-release SQL. Tests used an isolated random schema in a fresh private Unix-socket PostgreSQL cluster, not the application's database. Migration tests do not simulate a large production table or every hosted PostgreSQL configuration.

## Actual verification

[Recorded PostgreSQL checks](../evaluation/provider-admission-current.md): ten checks passed on PostgreSQL 18.4 (Homebrew), with zero provider calls.

- Three distinct admission processes attempted twelve background reservations against a daily eight-request limit with two reserved for interactive work: six background requests admitted, six denied, then two interactive requests admitted. Total remained eight.
- Three distinct processes attempted nine 700-token fixture reservations against a 2,800 estimated-token cap: four admitted, five denied, with exactly 2,800 reserved.
- Denial rollback, committed failure reservations, cancellation during a confirmed database lock wait, window resets, scoped status and additive migration/old SQL compatibility passed.
- Thirteen new unit regressions cover policy/defaults, token/priority boundaries, settings validation and the circuit-breaker distinction, including a mocked graph-denial followed by a successful interactive answer. Full backend suite: 378 passing tests. Nine frontend tests and TypeScript/Vite build pass.

These are admission-contract checks, not vendor token measurements, separate ingestion-worker recovery tests, a load benchmark or AI answer-quality evaluation. An initial test harness used overlapping coroutine patch contexts; it failed contention assertions. The harness was corrected to hold its configuration once per process before the recorded passing run.

## Reproduce without using application data

Use installed PostgreSQL binaries; the runner refuses network database targets and requires an owned private test socket directory.

```bash
admission_fixture=$(mktemp -d /private/tmp/conceptgraph-budget-pg.XXXXXX)
initdb -D "$admission_fixture/data" -U cg_budget --auth=trust --no-locale --encoding=UTF8
pg_ctl -D "$admission_fixture/data" -l "$admission_fixture/server.log" -o "-h '' -k $admission_fixture -p 55432" -w start
trap 'pg_ctl -D "$admission_fixture/data" -m fast -w stop' EXIT
.venv/bin/python scripts/evaluate_provider_admission.py --socket-dir "$admission_fixture"
.venv/bin/python -m unittest discover -s tests -q
```

The runner removes only its generated test schema. Stop the temporary server after testing; no application services, source documents or production stores are altered.
