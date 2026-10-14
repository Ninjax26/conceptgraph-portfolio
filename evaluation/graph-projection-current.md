# Graph projection — isolated Neo4j verification

Generated: 2026-10-14T10:59:32.621336+00:00.

Command: `.venv/bin/python scripts/evaluate_graph_projection.py`. Requires running Docker and an already-installed official `neo4j:5-community` image; no image pull.

7/7 checks passed against Neo4j 5.26.26. Hosted provider calls: **0**. Production mutations: **0**. The separate-network, loopback-bound test container had no host volume, disabled usage reporting, and was removed with its data and network.

| Check | Result |
| --- | --- |
| `merged_graph_replacement_preserves_other_uploads_and_ready_scope` | Pass |
| `replay_preserves_graph_content_without_duplicate_edges` | Pass |
| `real_transaction_rollback_after_delete_and_callback_failure` | Pass |
| `real_transaction_rollback_after_delete_and_cypher_failure` | Pass |
| `actual_driver_retries_entire_callback_after_injected_retryable_failure` | Pass |
| `repeated_cancellation_drains_actual_transaction_before_propagating` | Pass |
| `empty_replacement_removes_only_target_and_only_empty_course` | Pass |

## Limits

- No PostgreSQL/job fence, Qdrant/R2 or full ingestion worker exercised.
- Failures and graph outputs are authored; retry exception is injected, not a network outage.
- No hard-kill, ambiguous distributed commit, semantic accuracy or load-capacity claim.
- Automatic continuation remains disabled; this verifies its internal projection primitive only.

Image and source SHA-256 hashes are recorded in the adjacent JSON. Elapsed time is a test-run observation, not throughput or capacity.
