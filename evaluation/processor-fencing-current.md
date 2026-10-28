# Opt-in processor and deletion fencing — isolated stores

Generated: 2026-10-28T14:28:20.220090+00:00.

Command: `.venv/bin/python scripts/evaluate_upload_deletion.py --socket-dir <private-test-socket> --projection-fencing`.

18/18 checks passed. Zero hosted provider calls or production mutations. Temporary resources were removed.

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

## Limits

- ASGI HTTP requests exercise the real authenticated DELETE route; this is not an external network/Render test.
- PostgreSQL, Qdrant and Neo4j are real disposable stores; synthetic 3-dimensional vectors are inserted without embeddings.
- MinIO exercises real local S3 PUT/HEAD/DELETE requests, not hosted Cloudflare R2.
- Seven explicit process boundaries use SIGKILL; four additional boundaries raise exceptions. Other packet-loss/outage timings are untested.
- Recovery runs in a fresh process through production sweep methods, not the timer or a deployed worker.
- Automatic graph-only continuation remains disabled; this is not graph-job completion or an ingestion load benchmark.
- Existing in-flight queries that already loaded READY context are not cancelled by a subsequent deletion request.
- The opt-in processor checks use a native synthetic PDF, authored graph outputs and a fixed 3-dimensional encoder; no AI/OCR semantic quality is measured.
- Ingestion cancellation is graceful and drained, not a worker hard kill; delayed Qdrant writes after process death and legacy pre-rollout writers remain unqualified.
- Projection fencing is enabled only in the private fixture configuration; no production environment or automatic continuation is activated.

The adjacent JSON records exact crash observations, image IDs and source hashes.
