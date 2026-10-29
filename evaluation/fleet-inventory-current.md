# Separate-process read-only inventory — isolated stores

Generated: 2026-10-29T14:15:36.406119+00:00.

Command: `.venv/bin/python scripts/evaluate_upload_deletion.py --socket-dir <private-test-socket> --projection-fencing --worker-crashes --full-lifecycle --fleet-inventory`.

Audit option: `--vector-rollout-audit` (read-only auditor; fixture setup/teardown writes only owned synthetic data).

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
      "body_sha256": "662e54e549f4b6068c123195388bccc488b48e59990d25832ecd9c4906db289c",
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
      "body_sha256": "57a72a08dfb5c32db239bb500061ace58890a37bd3b520544b2118104ca542da",
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


## Full entry points and timer

- real_api_defers_ingestion_without_worker_and_enforces_auth_and_ready_gate: pass
- full_worker_sigkill_preserves_active_delete_guard_and_expired_presence_is_unavailable: pass
- fresh_actual_api_worker_exports_agree_without_claiming_fleet_or_drain: pass
- independent_postgres_schema_namespace_drift_is_not_compatible: pass
- independent_qdrant_collection_namespace_drift_is_not_compatible: pass
- independent_source_bucket_namespace_drift_is_not_compatible: pass
- independent_graph_default_database_namespace_drift_is_not_compatible: pass
- full_worker_recovers_and_http_query_citations_and_range_preview_use_ready_evidence: pass
- real_retention_timer_keeps_empty_tracking_then_cleans_post_delete_delayed_put: pass
- deleted_source_stays_inaccessible_and_four_stores_preserve_neighbor_upload: pass
- sigterm_drains_full_worker_and_removes_registry_presence: pass
- sigterm_completes_actual_api_lifespan_before_uvicorn_reraises_signal: pass

```json
{
  "fleet_inventory": {
    "supplied_peer_comparison": {
      "supplied_instances_compatible": true,
      "fleet_completeness_verified": false,
      "writer_drain_verified": false,
      "activation_allowed": false,
      "differences": []
    },
    "independent_namespace_drift": {
      "postgres_schema": [
        "postgres"
      ],
      "qdrant_collection": [
        "qdrant"
      ],
      "source_bucket": [
        "source"
      ],
      "graph_default_database": [
        "neo4j"
      ]
    },
    "actual_peer_count": 2,
    "drift_probes_are_controller_observations_not_extra_running_workers": true,
    "physical_identity_permissions_and_unknown_instances_verified": false,
    "source_head_does_not_create_buckets": true,
    "hosted_model_calls": 0
  },
  "empty_timer_wait_seconds": 56.318,
  "late_cleanup_timer_wait_seconds": 59.965,
  "configured_cleanup_interval_seconds": 60,
  "original_worker_exit_code": -9,
  "replacement_worker_exit_code": 0,
  "lease_and_presence_expiry_advanced_in_owned_fixture": true,
  "cleanup_due_time_advanced_in_owned_fixture": true,
  "manual_sweep_calls": 0,
  "manual_vector_cleanup_calls": 0,
  "hosted_model_calls": 0,
  "transport_only_proxy_kept_logical_store_binding_identical": true,
  "api_exit_code_after_completed_asgi_shutdown": -15
}
```


## Read-only vector audit

- paginated_current_payloads_accept_legacy_and_scoped_physical_ids: pass
- postgres_attempt_history_distinguishes_known_stale_execution: pass
- unknown_payloads_do_not_certify_cleanup_coverage: pass
- untagged_payloads_do_not_certify_cleanup_coverage: pass
- missing_current_chunks_fail_even_when_some_vectors_exist: pass
- duplicate_logical_chunk_is_not_additional_current_coverage: pass
- wrong_course_payloads_fail_closed: pass
- bounded_partial_scan_is_not_complete_coverage: pass
- peer_vector_contract_drift_stops_before_remote_reads: pass
- only_metadata_is_read_and_all_audits_preserve_postgres_vectors_and_neighbor: pass

```json
{
  "compatible": {
    "scan_completed": true,
    "read_coverage_ok": true,
    "cleanup_payloads_trackable": true,
    "activation_allowed": false,
    "writer_drain_verified": false,
    "error_type": null,
    "counts": {
      "scanned": 3,
      "current_points": 3,
      "current_unique_chunks": 3,
      "known_stale_points": 0,
      "unknown_task_points": 0,
      "untagged_points": 0,
      "foreign_points": 0,
      "malformed_current_points": 0,
      "duplicate_current_chunks": 0
    }
  },
  "known_stale": {
    "scan_completed": true,
    "read_coverage_ok": true,
    "cleanup_payloads_trackable": true,
    "activation_allowed": false,
    "writer_drain_verified": false,
    "error_type": null,
    "counts": {
      "scanned": 4,
      "current_points": 3,
      "current_unique_chunks": 3,
      "known_stale_points": 1,
      "unknown_task_points": 0,
      "untagged_points": 0,
      "foreign_points": 0,
      "malformed_current_points": 0,
      "duplicate_current_chunks": 0
    }
  },
  "partial": {
    "scan_completed": false,
    "read_coverage_ok": false,
    "cleanup_payloads_trackable": false,
    "activation_allowed": false,
    "writer_drain_verified": false,
    "error_type": null,
    "counts": {
      "scanned": 2,
      "current_points": 2,
      "current_unique_chunks": 2,
      "known_stale_points": 0,
      "unknown_task_points": 0,
      "untagged_points": 0,
      "foreign_points": 0,
      "malformed_current_points": 0,
      "duplicate_current_chunks": 0
    }
  },
  "read_only_client_calls": 35,
  "model_calls": 0,
  "production_data_mutations": 0,
  "postgres_neo4j_r2_fleet_compatibility_verified": false,
  "writer_drain_verified": false
}
```


## Current-schema startup

- current_schema_restart_completes_while_existing_recovery_reader_stays_open: pass

## Limits

- The deletion matrix uses in-process ASGI requests; full-lifecycle mode additionally uses real loopback HTTP. Neither is a Render test.
- PostgreSQL, Qdrant and Neo4j are real disposable stores; synthetic 3-dimensional vectors are inserted without embeddings.
- MinIO exercises real local S3 PUT/HEAD/DELETE requests, not hosted Cloudflare R2.
- Seven explicit process boundaries use SIGKILL; four additional boundaries raise exceptions. Other packet-loss/outage timings are untested.
- Deletion-matrix recovery uses fresh processes calling production sweeps. Full-lifecycle mode additionally exercises the real local worker and retention timer; neither qualifies deployed-worker operation.
- Automatic graph-only continuation remains disabled; this is not graph-job completion or an ingestion load benchmark.
- Existing in-flight queries that already loaded READY context are not cancelled by a subsequent deletion request.
- The opt-in processor checks use a native synthetic PDF, authored graph outputs and a fixed 3-dimensional encoder; no AI/OCR semantic quality is measured.
- Two coordinator-only schedules kill subprocesses after their complete PUT reaches the loopback proxy. Owned lease expiry is advanced and fresh coordinators recover to READY. These schedules do not exercise full entry points; full-lifecycle mode records its additional separate API/worker experiment independently. Arbitrary network faults and load remain untested.
- A failed late-deletion qualification blocks activation; explicitly manual fixture cleanup is not application recovery. Mixed legacy writers remain unqualified.
- Projection fencing is enabled only in the private fixture configuration; no production environment or automatic continuation is activated.
- Full-lifecycle mode runs real local HTTP and worker entry points with an actual 60-second retention timer; embedding/graph/reranking/synthesis are authored adapters, not semantic or provider-capacity measurements. Fixture lease/presence/due timestamps are advanced, but lifecycle and timer methods are not mocked.
- Vector rollout audits inspect caller-supplied configuration and bounded metadata snapshots. They do not prove historical embedding origin, physical endpoint identity, PostgreSQL/Neo4j/R2 fleet compatibility, concurrent snapshot stability or legacy-writer drain; they never authorize activation or repair payloads.
- Optional fleet inventory captures the two actual local API/worker processes and controller drift probes using READ ONLY SQL, database info, collection schema and HEAD. Matching supplied observations do not discover unknown processes, prove physical identity/equal permissions or certify drain/activation. No production inventory is collected.

The adjacent JSON records exact crash observations, image IDs and source hashes.
