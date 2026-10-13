# READY/snapshot staging — isolated transaction checks

Command: `.venv/bin/python scripts/evaluate_graph_snapshot_staging.py --socket-dir <private-test-socket>`.

9 checks passed. Hosted provider calls: 0. Production test-data mutations: 0.

- default_completion_does_not_create_job: passed
- ready_attempt_and_snapshot_become_visible_together: passed
- ready_identity_scope_and_persisted_reset_preserved: passed
- failure_after_flush_rolls_back_ready_attempt_and_job: passed
- same_fenced_execution_can_complete_after_rollback: passed
- invalid_source_snapshot_cannot_partially_commit_ready: passed
- existing_ingestion_lease_fence_precedes_snapshot_staging: passed
- demo_batch_budget_does_not_stage_provider_job: passed
- restart_preserves_atomically_staged_jobs: passed

## Limits

- Committed index/source/graph counts are fixtures; no full processor, provider or cross-store consistency test.
- Staging flag defaults off; dispatcher, graph-only worker and atomic graph publication remain unimplemented.
- New snapshot storage/retention budgets and full split-worker/hard-kill checks remain acceptance gates.
- Rollback is an injected pre-commit failure, not a simulation of an ambiguous network failure after commit.
