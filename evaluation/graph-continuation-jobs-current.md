# Graph continuation jobs — isolated metadata checks

Command: `.venv/bin/python scripts/evaluate_graph_continuation_jobs.py --socket-dir <private-test-socket>`.

47 checks passed. Hosted provider calls: 0. Production test-data mutations: 0.

- additive_schema_restart_and_due_lease_indexes: passed
- database_rejects_incomplete_running_lease_and_excess_attempts: passed
- persisted_delay_and_idempotent_scheduling_no_hot_retry: passed
- three_processes_one_claim_with_ready_identity_and_scope_unchanged: passed
- independent_claim_token_and_owner_fence_checkpoint_writes: passed
- recovery_checkpoint_is_source_concept_and_claim_fenced: passed
- fenced_read_uses_persisted_snapshot_filters_legacy_and_releases_locks: passed
- persisted_empty_recovery_and_sections_resume_without_provider_calls: passed
- graph_only_stage_renews_real_lease_and_resumes_saved_batches: passed
- publication_preflight_keeps_document_fence_and_rejects_stale_claim: passed
- publication_replay_uses_current_sources_keeps_fence_and_rejects_stale_claim: passed
- draft_same_transaction_idempotency_rollback_and_restart_preservation: passed
- draft_logical_byte_and_count_limits_preserve_idempotent_saved_candidate: passed
- two_concurrent_sessions_admit_one_draft_at_last_slot_without_ready_mutation: passed
- intent_stages_only_from_validated_draft_and_survives_restart: passed
- receipt_requires_exact_observed_marker_and_survives_restart: passed
- intent_and_receipt_survive_document_and_job_cascade_for_reconciliation: passed
- deleted_source_intent_discovery_excludes_live_uploads_and_is_repeatable: passed
- failed_remote_tombstone_leaves_no_cleanup_receipt: passed
- deleted_source_reconciliation_retries_after_remote_failure: passed
- cleanup_receipt_survives_restart_and_prevents_duplicate_remote_calls: passed
- cleaned_deleted_source_does_not_starve_later_discovery: passed
- concurrent_reconcilers_tombstone_once_and_record_one_receipt: passed
- deleted_source_reconciliation_requires_committed_absence: passed
- uncommitted_deletion_cannot_authorize_remote_tombstone: passed
- draft_conflict_and_database_storage_constraint_fail_closed: passed
- job_deletion_cascades_unpublished_draft_without_changing_ready_result_and_rolls_back: passed
- changed_section_concepts_invalidate_recovery_save: passed
- changed_section_concepts_invalidate_recovery_read: passed
- graph_only_stage_durably_defers_known_quota_without_hot_retry: passed
- defer_persists_reset_and_invalidates_old_claim_without_changing_ready_result: passed
- killed_extraction_stage_recovers_after_fixture_lease_expiry_and_reuses_checkpoints: passed
- attempt_limit_stops_automatic_requeue: passed
- duplicate_scheduling_cannot_restart_exhausted_job: passed
- tampered_snapshot_is_stopped_without_reparsing_or_provider_calls: passed
- changed_model_contract_requires_explicit_rebuild: passed
- superseded_document_task_stops_old_job: passed
- expired_retention_is_not_resurrected: passed
- deletion_lock_skipped_and_fk_cascade_fences_old_claim: passed
- batch_budget_only_result_does_not_schedule_provider_retry: passed
- old_document_sql_and_schema_restart_preserve_jobs: passed
- logical_jsonb_byte_budget_enforces_exact_boundary: passed
- concurrent_snapshot_admission_cannot_exceed_retained_job_cap: passed
- terminal_snapshot_cleanup_rolls_back_and_preserves_exhausted_tombstone: passed
- terminal_draft_cleanup_skips_document_locked_by_another_session: passed
- terminal_draft_cleanup_is_bounded_repeatable_rollback_safe_and_preserves_job_history: passed
- source_generation_migration_is_additive_repeatable_and_preserves_legacy_rows: passed

## Limits

- Claim processes and a SIGKILL extraction-stage process use mocked AI, not full API/ingestion-worker operation.
- Killed-process recovery uses an explicit expired-lease fixture, not a natural lease-duration wait.
- Source/vector/graph counts and extracted graph output are authored fixtures; no Qdrant, Neo4j or R2 consistency claim.
- Scheduler/graph-only worker/recovery projection are not connected; no production automatic continuation is enabled.
- An immutable intent, observed-marker receipt and deleted-source cleanup receipt survive document/job deletion; source-generation migration is nullable for legacy rows, which are rejected for publication replay. The marker reader and deleted-source reconciler's Neo4j tombstone are mocked. No runtime publisher, completion or reconciliation dispatcher consumes them. Draft admission concurrency uses two sessions in one process, not full independent workers; limits/cleanup are default-disabled/unscheduled and logical, not physical storage measurements.
- Optional logical snapshot budgets and terminal-body cleanup are tested but default-disabled/unscheduled; JSON text bytes are not physical disk, WAL or memory measurements.
- Observed test duration is not an ingestion throughput, semantic-quality or production capacity metric.
