# Phase 3: gated idle-worker graph dispatch

Spec: current implementation ledger and agreed Phase 3 provider-safe ingestion.
Continue in the existing relocated checkout, with explicit scoped origin release
authorization. No production flag activation, hosted AI, schema or API changes.

1. Characterize a separate default-false dispatch flag; require snapshot and
   projection fences, worker/combined role, shared provider budgets and positive
   interactive request headroom in both windows (and token headroom when enabled).
2. Let the existing bounded worker slots check one due job only after an idle
   document-queue timeout. Do not enqueue leased graph claims, add independent
   worker concurrency, or preempt work already underway. Queued PDF ingestion
   wins before a new graph claim. Preserve single-attempt failures and durable
   provider deadlines. Drain claim writes and processor publication on shutdown.
3. Unit RED→GREEN checks for gates, queue priority, shared concurrency, expected
   deferral, failures, repeated cancellation and stop/restart ownership.
4. Owned PostgreSQL/Neo4j checks through actual coordinator workers with authored
   extraction: competing idle coordinators, draft replay, fresh snapshot work,
   persisted quota wait, and shutdown during publication. No hosted requests.
5. Full suite/frontend/build/hash/diff checks; one fresh read-only final review,
   one regression fix pass if needed; scoped push and free-service verification.

Pre-flight: worker dispatch consumes the already-qualified one-claim processor;
claim commits must finish before cancellation releases lifecycle ownership.
Existing source/job lease and immutable draft/marker services remain authoritative.
Ruling: this increment qualifies idle-slot dispatch only, with production gates
still false. Exhausted-publication discovery/reconciliation, terminal retention,
full combined hard-kill qualification, rollout and provider/cache completeness
remain the next checkpoint. No Phase 3 completion claim is authorized.

Progress: baseline 657 tests passed (3.836s); 11 new contracts failed on the
missing setting then passed (0.184s). Full suite 668 (4.366s), nine frontend tests,
TS/Vite build (5.72s) and 19 owned PostgreSQL/Neo4j checks pass. All 18 source hashes
match and compile. Integration coordinators share one event loop, no separate OS
worker claim; five prior publisher kills remain separately scoped. Initial npm
PATH and stopped Docker attempts failed; corrected/reran, no success invented.
Owned resources removed; no hosted calls/production fixtures/paid resources.
The one fresh read-only final review found no important findings. No fix pass,
rulings or deferred minors. Final suite 668 tests (4.989s) and diff checks pass.
Release verified: origin/main 1d37749026cf5ee3d9aa0f2857c7ac1e4eece3f2;
GitHub Verify 41088598995 success; free API exact SHA, ready/combined, queue zero,
no degraded services. Public sample API 200 and saved frontend 3-node/2-edge
illustration renders without captured console errors. Frontend SHA not exposed.
No production activation/hosted inference/paid service. Keep this post-release
note local until a substantive later commit; Phase 3 remains partial.
