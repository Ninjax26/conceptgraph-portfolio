# Durable vector cleanup and killed-worker recovery

Generated: 2026-10-28T17:30:48.587474+00:00.

Command: `.venv/bin/python scripts/evaluate_upload_deletion.py --socket-dir <private-test-socket> --projection-fencing --worker-crashes`.

Overall qualification: **PASS**.

18/18 deletion regression checks passed. Zero hosted provider calls or production mutations. Temporary resources were removed.

- unauthenticated_delete_preserves_all_stores: pass
- authenticated_delete_removes_target_and_preserves_neighbor: pass
- uncommitted_deletion_preserves_ready_document_and_all_artifacts: pass
- hard_kill_intent_resumes_in_new_process_without_ready_leak: pass
- concurrent_recovery_processes_finish_one_removal: pass
- hard_kill_vectors_resumes_in_new_process_without_ready_leak: pass
- hard_kill_graph_resumes_in_new_process_without_ready_leak: pass
- hard_kill_storage_resumes_in_new_process_without_ready_leak: pass
- hard_kill_before_metadata_commit_resumes_in_new_process_without_ready_leak: pass
- committed_deletion_blocks_late_graph_replay_before_reconciliation: pass
- failure_vectors_preserves_durable_retry: pass
- failure_graph_preserves_durable_retry: pass
- failure_storage_preserves_durable_retry: pass
- failure_before_metadata_commit_preserves_durable_retry: pass
- shared_source_survives_until_last_reference_is_deleted: pass
- concurrent_shared_source_deletion_leaves_no_orphan: pass
- removed_checkpoints_and_markers_leave_only_protected_upload: pass
- unrelated_ready_upload_survives_all_crash_and_failure_scenarios: pass

Processor checks: 10 passed.

- native_pdf_full_processor_reaches_ready_with_real_stores_and_authored_ai: pass
- delayed_old_vector_replay_cannot_overwrite_current_processor_execution: pass
- real_ready_task_scope_filters_dense_and_identifier_candidates_before_limit: pass
- legacy_uuid_with_current_execution_payload_retains_pdf_citation_identity: pass
- real_manual_retry_advances_graph_generation_and_rejects_older_marked_replay: pass
- stale_processor_source_cannot_enter_graph_or_vector_cleanup: pass
- failed_processor_closes_attempt_and_removes_only_its_vector_payloads: pass
- authenticated_delete_tombstones_baseline_without_publication_intent: pass
- repeated_cancellation_drains_graph_and_source_lock_then_real_recovery_uses_new_attempt: pass
- processor_fixtures_remove_all_vector_and_source_artifacts_through_real_delete_route: pass

## Durable cleanup checks

- additive_schema_reversal_and_reinitialization_preserve_existing_source_and_attempts: pass
- staging_and_removal_flag_roll_back_together_before_commit: pass
- committed_tracking_is_idempotent_and_includes_all_source_attempts: pass
- live_source_blocks_cleanup_and_tracking_survives_source_cascade: pass
- remote_cleanup_filters_both_upload_and_execution_preserving_neighbor_and_other_tasks: pass
- store_binding_mismatch_cannot_redirect_cleanup: pass
- ambiguous_delete_ack_retains_obligation_and_replay_is_cleaned_on_retry: pass
- recreated_source_blocks_retained_old_cleanup: pass
- concurrent_reconciler_yields_to_existing_claim: pass
- repeated_cancellation_drains_remote_delete_before_releasing_admission: pass
- acknowledged_cleanup_keeps_tracking_for_future_late_writes: pass
- database_due_discovery_respects_explicit_batch_bound: pass

## Worker crash observations

- sigkill_after_restart_releases_source_lock_after_accepted_put: pass
- fresh_coordinator_after_restart_recovers_second_attempt_to_ready: pass
- fresh_coordinator_recovers_new_attempt_and_excludes_post_sigkill_old_put: pass
- deletion_after_delayed_put_removes_both_attempts: pass
- sigkill_after_completed_delete_releases_source_lock_after_accepted_put: pass
- fresh_coordinator_after_completed_delete_recovers_second_attempt_to_ready: pass
- deleted_source_stays_ineligible_after_post_sigkill_late_put: pass
- tracking_survives_empty_sweep_and_fresh_process_removes_late_vector: pass
- tracking_survives_success_and_removes_subsequent_point_replay: pass
- retry_cooldown_prevents_immediate_repeat_cleanup: pass

```json
{
  "after_restart": {
    "worker_exit_code": -9,
    "captured_points": 1,
    "delivery": {
      "point_count": 1,
      "body_sha256": "de494450fc1b67b9abb841d3d4afd420b16b092372ae7bd9fa3c670651dac60b",
      "http_status": 200,
      "completed": true
    },
    "attempt_count": 2,
    "current_vectors_preserved": 1,
    "old_vectors_excluded_from_ready_search": true
  },
  "after_completed_delete": {
    "worker_exit_code": -9,
    "attempt_count": 2,
    "before_delivery": {
      "metadata": false,
      "eligible": false,
      "vectors": 0,
      "concepts": 0,
      "source": false
    },
    "empty_runtime_sweep_before_delivery": {
      "removed": 0,
      "receipts": 0,
      "vectors_checked": 10
    },
    "delivery": {
      "point_count": 1,
      "body_sha256": "69791b5664e36d58331bd0f8cdaaac29b25d83981f417d0bcffe03096a3366bf",
      "http_status": 200,
      "completed": true
    },
    "after_delivery": {
      "metadata": false,
      "eligible": false,
      "vectors": 1,
      "concepts": 0,
      "source": false
    },
    "cooldown_sweep": {
      "removed": 0,
      "receipts": 0,
      "vectors_checked": 0
    },
    "fresh_runtime_sweep": {
      "removed": 0,
      "receipts": 0,
      "vectors_checked": 2
    },
    "after_runtime_sweep": {
      "metadata": false,
      "eligible": false,
      "vectors": 0,
      "concepts": 0,
      "source": false
    },
    "manual_point_replay_after_successful_cleanup": true,
    "repeated_runtime_sweep": {
      "removed": 0,
      "receipts": 0,
      "vectors_checked": 2
    },
    "after_repeated_runtime_sweep": {
      "metadata": false,
      "eligible": false,
      "vectors": 0,
      "concepts": 0,
      "source": false
    },
    "activation_safe": true,
    "manual_fixture_vector_cleanup_confirmed": false
  }
}
```


## Limits

- ASGI HTTP requests exercise the real authenticated DELETE route; this is not an external network/Render test.
- PostgreSQL, Qdrant and Neo4j are real disposable stores; synthetic 3-dimensional vectors are inserted without embeddings.
- MinIO exercises real local S3 PUT/HEAD/DELETE requests, not hosted Cloudflare R2.
- Seven explicit process boundaries use SIGKILL; four additional boundaries raise exceptions. Other packet-loss/outage timings are untested.
- Recovery runs in a fresh process through production sweep methods, not the timer or a deployed worker.
- Automatic graph-only continuation remains disabled; this is not graph-job completion or an ingestion load benchmark.
- Existing in-flight queries that already loaded READY context are not cancelled by a subsequent deletion request.
- The opt-in processor checks use a native synthetic PDF, authored graph outputs and a fixed 3-dimensional encoder; no AI/OCR semantic quality is measured.
- Two coordinator subprocesses are killed after the loopback proxy receives their complete PUT, before forwarding to Qdrant. Each owned PostgreSQL lease expiry is advanced; fresh coordinators perform actual recovery/claiming to READY. This is not the complete app.worker lifecycle, a separate API, arbitrary network faults or a load benchmark.
- A failed late-deletion qualification blocks activation; explicitly manual fixture cleanup is not application recovery. Mixed legacy writers remain unqualified.
- Projection fencing is enabled only in the private fixture configuration; no production environment or automatic continuation is activated.

The adjacent JSON records exact crash observations, image IDs and source hashes.
