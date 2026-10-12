# Phase 3 provider admission check

Real isolated PostgreSQL; three separate admission processes per contention case; no provider calls.

- concurrent_additive_migration_preserves_legacy_counts: passed
- old_release_sql_compatible_without_dropping_usage: passed
- separate_process_request_holdback_and_total_cap: passed
- separate_process_token_cap_no_overspend: passed
- denial_rolls_back_insert_and_releases_lock: passed
- failed_attempt_retains_committed_reservation: passed
- cancelled_lock_wait_no_reservation_or_leaked_lock: passed
- expired_windows_reset_counts_and_estimates_together: passed
- minute_reset_preserves_current_daily_usage: passed
- status_scoped_and_matches_committed_usage: passed

## Measurements

- request_priority: `{"background_accepted": 6, "background_denied": 6, "interactive_accepted": 2, "processes": 3, "reserved_estimated_tokens": 3200, "total_requests": 8}`
- token_admission: `{"accepted": 4, "denied": 5, "processes": 3, "reserved_estimated_tokens": 2800, "total_requests": 4}`

## Limits

- Real PostgreSQL and separate admission processes, not separate API/ingestion workers.
- No hosted LLM/embedding/reranking/R2/Neo4j calls; no production load or answer-quality claim.
- Token reservations are explicit fixture estimates, not vendor-reported usage.
- Existing application budgets/data were not accessed; the isolated random schema was removed.
- Estimated-token limits and interactive reserves default to zero (opt-in).
