# Isolated document deletion recovery

Generated: 2026-10-27T11:30:25.684284+00:00.

Command: .venv/bin/python scripts/evaluate_upload_deletion.py --socket-dir <private-test-socket>.

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
- committed_deletion_discovers_late_graph_write_and_blocks_replay: pass
- failure_vectors_preserves_durable_retry: pass
- failure_graph_preserves_durable_retry: pass
- failure_storage_preserves_durable_retry: pass
- failure_before_metadata_commit_preserves_durable_retry: pass
- shared_source_survives_until_last_reference_is_deleted: pass
- concurrent_shared_source_deletion_leaves_no_orphan: pass
- removed_checkpoints_and_markers_leave_only_protected_upload: pass
- unrelated_ready_upload_survives_all_crash_and_failure_scenarios: pass

## Limits

- ASGI HTTP requests exercise the real authenticated DELETE route; this is not an external network/Render test.
- PostgreSQL, Qdrant and Neo4j are real disposable stores; synthetic 3-dimensional vectors are inserted without embeddings.
- MinIO exercises real local S3 PUT/HEAD/DELETE requests, not hosted Cloudflare R2.
- Seven explicit process boundaries use SIGKILL; four additional boundaries raise exceptions. Other packet-loss/outage timings are untested.
- Recovery runs in a fresh process through production sweep methods, not the timer or a deployed worker.
- Automatic graph-only continuation remains disabled; this is not graph-job completion or an ingestion load benchmark.
- Existing in-flight queries that already loaded READY context are not cancelled by a subsequent deletion request.

The adjacent JSON records exact crash observations, image IDs and source hashes.
