# Graph runtime lifecycle — owned PostgreSQL and Neo4j

Command: `.venv/bin/python scripts/evaluate_graph_publication_recovery.py --socket-dir <private-test-socket> --graph-runtime`.

- real_marked_publication_and_postgresql_completion: passed
- processor_saved_draft_replay_skips_extraction_and_preserves_ready_identity: passed
- processor_fresh_authoritative_snapshot_stages_and_publishes_under_real_locks: passed
- processor_quota_defers_durably_without_draft_or_projection_changes: passed
- idle_competing_coordinators_claim_saved_draft_once_without_extraction: passed
- idle_worker_extracts_authoritative_snapshot_and_completes_real_projection: passed
- idle_worker_respects_durable_quota_deadline_across_multiple_polls: passed
- idle_worker_shutdown_drains_remote_receipt_unit_before_releasing_source_lock: passed
- maintenance_retention_preserves_old_unreceipted_intent_recovery_bodies: passed
- maintenance_exhausted_discovery_has_bounded_stable_cursor_and_excludes_other_jobs: passed
- maintenance_recovers_exact_final_marker_and_keeps_missing_marker_stopped: passed
- maintenance_prunes_receipted_terminal_bodies_preserving_ready_tombstone_and_checkpoints: passed
- maintenance_actual_idle_worker_recovers_final_receipt_and_drains_sweeper: passed
- worker_entrypoint_sigkill_remote_commit_natural_final_lease_recovery: passed
- combined_entrypoint_sigkill_remote_commit_natural_final_lease_recovery: passed
- sigkill_before_intent_commit_recovers_without_duplicate_remote_write: passed
- sigkill_after_intent_commit_recovers_without_duplicate_remote_write: passed
- sigkill_during_remote_transaction_recovers_without_duplicate_remote_write: passed
- sigkill_after_remote_commit_recovers_without_duplicate_remote_write: passed
- sigkill_after_result_commit_recovers_without_duplicate_remote_write: passed
- lost_remote_acknowledgement_reuses_exact_marker: passed
- repeated_cancellation_drains_full_unit_before_releasing_source_lock: passed
- competing_publishers_commit_one_projection_and_one_receipt: passed
- committed_source_removal_in_intent_gap_blocks_remote_publication: passed
- conflicting_remote_marker_fails_closed_without_replacement: passed
- provider_limited_candidate_never_writes_remote_projection: passed

Limits:

- Authored source/checkpoint/graph fixtures; no model, embedding, Qdrant, R2 or live HTTP ingestion.
- Five direct-publisher crash cases explicitly expire the fixture lease; these are not natural waiting measurements.
- Source removal in the intent gap covers PostgreSQL and Neo4j; full API/Qdrant/object-store deletion is checked separately.
- Hard kills cover named instrumented boundaries, not every delayed server commit or network schedule.
- Production dispatch remains default-off; no capacity, throughput or semantic accuracy claim.
- Four OS processes exercise worker entrypoint and combined ASGI lifespan with natural 60-second final lease expiry after a real remote graph commit.
- Qdrant startup validation, retention timer and close hooks are stubbed in this graph fixture; no HTTP or four-store graph-runtime benchmark.
- Other graph-worker kill boundaries and full activation/rollout remain unqualified.
