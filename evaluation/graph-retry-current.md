# Durable graph retry eligibility check

Real isolated PostgreSQL and retry API; no provider calls.

- deadline_persisted_from_database_clock: passed
- fresh_process_cannot_bypass_persisted_deadline: passed
- premature_concurrent_api_retries_preserve_ready_state_attempts_and_checkpoint: passed
- ready_document_remains_query_eligible_during_wait: passed
- expired_deadline_allows_exactly_one_concurrent_attempt: passed
- checkpoint_survives_retry_and_stale_task_is_fenced: passed
- cached_unclaimed_read_cannot_steal_current_worker_lease: passed
- waiting_ready_document_deletion_cascades_checkpoint: passed
- legacy_record_without_deadline_keeps_existing_retry_behavior: passed
- cached_ready_deletion_read_cannot_delete_new_active_attempt: passed

## Limits

- Real PostgreSQL and ASGI endpoints, with source-existence and enqueue test doubles.
- A separate process enforces the persisted deadline; no real API/ingestion-worker or hard-kill test.
- Auth and middleware are covered by the normal suite; this isolated ASGI app tests retry endpoints only.
- Committed vector/graph counts are authored fixtures; no Qdrant, Neo4j or R2 mutation or consistency claim.
- Automatic delayed graph continuation is not implemented; this increment prevents premature manual retries.
