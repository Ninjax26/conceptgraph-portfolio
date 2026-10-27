# Graph projection — isolated Neo4j verification

Generated: 2026-10-27T07:50:28.237594+00:00.

Command: `.venv/bin/python scripts/evaluate_graph_projection.py`. Requires running Docker and an already-installed official `neo4j:5-community` image; no image pull.

19/19 checks passed against Neo4j 5.26.26. Hosted provider calls: **0**. Production mutations: **0**. The separate-network, loopback-bound test container had no host volume, disabled usage reporting, and was removed with its data and network.

| Check | Result |
| --- | --- |
| `merged_graph_replacement_preserves_other_uploads_and_ready_scope` | Pass |
| `replay_preserves_graph_content_without_duplicate_edges` | Pass |
| `marker_commits_atomically_and_replay_reads_exact_identity` | Pass |
| `marker_identity_conflict_rolls_back_projection` | Pass |
| `upload_cleanup_removes_only_matching_remote_marker_and_is_idempotent` | Pass |
| `exact_obsolete_publication_cleanup_is_idempotent_and_preserves_neighbors` | Pass |
| `obsolete_marker_cleanup_preserves_newer_execution_projection` | Pass |
| `late_old_marker_replay_cannot_overwrite_newer_task_projection` | Pass |
| `same_generation_different_task_cannot_replace_projection` | Pass |
| `same_generation_different_payload_cannot_replace_projection` | Pass |
| `tombstone_blocks_late_publication_and_preserves_neighbors` | Pass |
| `tombstone_before_first_marker_blocks_publication` | Pass |
| `tombstone_rejects_foreign_course_without_partial_cleanup` | Pass |
| `concurrent_tombstone_waits_for_inflight_publication_then_removes_it` | Pass |
| `real_transaction_rollback_after_delete_and_callback_failure` | Pass |
| `real_transaction_rollback_after_delete_and_cypher_failure` | Pass |
| `actual_driver_retries_entire_callback_after_injected_retryable_failure` | Pass |
| `repeated_cancellation_drains_actual_transaction_before_propagating` | Pass |
| `empty_replacement_removes_only_target_and_only_empty_course` | Pass |

## Limits

- No PostgreSQL/job fence, Qdrant/R2 or full ingestion worker exercised.
- Failures and graph outputs are authored; retry exception is injected, not a network outage.
- No hard-kill, ambiguous distributed commit, semantic accuracy or load-capacity claim.
- The generation fence rejects a late old marked write after a newer attempt; it has no worker caller yet.
- The live deletion route does not call the tombstone primitive; it is reserved for a reconciler that has proved committed source deletion. Do not activate continuation until source authority and cross-store recovery are connected.
- Automatic continuation remains disabled; this verifies its internal projection primitive only.

Image and source SHA-256 hashes are recorded in the adjacent JSON. Elapsed time is a test-run observation, not throughput or capacity.
