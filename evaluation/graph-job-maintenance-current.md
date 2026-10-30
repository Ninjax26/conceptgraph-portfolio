# Graph job maintenance — local PostgreSQL and Neo4j

Command: `.venv/bin/python scripts/evaluate_graph_publication_recovery.py --socket-dir <private-test-socket> --graph-maintenance`.

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

- Synthetic source/checkpoint/graph fixtures; no model, embedding, Qdrant, R2 or live API ingestion.
- Crash recovery explicitly expires the isolated fixture lease before a fresh claim; no natural lease-duration or full worker benchmark.
- Source removal in the intent gap uses PostgreSQL and Neo4j only; full API/Qdrant/R2 deletion is covered separately.
- Hard kills cover five instrumented boundaries, not every network timing or delayed server commit.
- Existing unmarked ingestion/retry writes, superseded source generations and combined/separate OS graph-worker recovery still require verification before activation.
- Production dispatch defaults off. Optional idle-coordinator checks share one event loop, not separate OS workers or a hard-kill worker benchmark. Test elapsed time is not throughput or capacity.
