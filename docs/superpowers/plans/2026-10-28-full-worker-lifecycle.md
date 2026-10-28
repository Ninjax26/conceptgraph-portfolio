# Full local API/worker lifecycle and cleanup timer

Use superpowers:executing-plans inline; one fresh-context final review.

**Goal:** Qualify the existing default-off safety integration through real `app.main`/Uvicorn and `app.worker.run_worker` processes rather than direct coordinator/sweep calls.

**Ruling:** Reuse the owned four-store fixture. Start an API-only process with an inherited loopback listener. Save an authenticated native synthetic PDF while no worker exists; verify deferred processing/READY refusal. Start the real worker lifecycle with offline graph/vector adapters and a transport-only delayed PUT proxy, keeping its logical store configuration identical to the API. SIGKILL it, advance only its owned document lease/presence expiry, then start a fresh full worker. Verify recovery, registry/readiness, real HTTP query/citations/preview, active deletion guard and source isolation. Delete the recovered source and let the actual retention loop perform an empty check before releasing the accepted old request. Advance only fixture tombstone due times and wait for the next real timer cycle to remove the late vector. Use the valid minimum 60-second cleanup interval, not a mocked timer. SIGTERM both processes and verify registry/resources close.

**Constraints:** Both production flags remain disabled; test configurations opt in only locally. API/worker processes share the private schema and configured stores. Native parsing, HTTP/storage/auth/metadata/graph operations are real. Embeddings, graph extraction, reranking and synthesis are explicitly authored substitutes; no hosted AI or semantic/capacity claims. No paid services, production data, private PDFs, secrets or unrelated files. Retain all existing local work and historical reports.

- [x] RED/GREEN fixture contracts for roles, production defaults, valid timer settings and labelled offline answer adapters.
- [x] Add resource/model-only dependency injection around the actual entry points, without mocking their start/stop/retention logic.
- [x] Execute the complete split-process/auth/recovery/timer/shutdown experiment with owned fixture cleanup; preserve earlier reports separately.
- [x] Run relevant/full tests, compile/diff and exact source hashes; one fresh review and focused correction if needed.
- [x] Record true results, failure modes and remaining Phase 3 gates. Do not activate flags or equate this workload with capacity or provider-priority qualification.

Result (29 October India): `evaluation/worker-lifecycle-current.md` records PASS for 57 checks and ten SIGKILLs. Actual 60-second timer waits were 59.352/59.955 seconds; no manual sweep was credited. All four stores preserve the neighbor. 606 backend tests, five focused fixture contracts, nine frontend tests, TypeScript/Vite build, 36 exact source hashes/compile checks and diff pass. A fresh read-only review and small follow-up found no blockers. The first full run had an incorrect API exit-0 expectation; installed Uvicorn re-raises SIGTERM after actual ASGI shutdown, so the final fixture observes shutdown completion and expects -15. No production lifecycle code was changed to satisfy this test. Production flags remain off; deployment compatibility/legacy drain, realistic load, hosted stores, provider scheduling/accounting and cache remain separate gates.
