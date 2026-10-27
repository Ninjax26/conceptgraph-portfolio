# Graph deletion recovery — isolated PostgreSQL and Neo4j

Generated: 2026-10-27T10:26:30.412765+00:00.

Command: `.venv/bin/python scripts/evaluate_graph_cross_store_recovery.py --socket-dir <private-test-socket>`. Requires a disposable local PostgreSQL cluster and an already-installed official Neo4j image.

3/3 checks passed. Hosted provider calls: **0**. PostgreSQL schema, Neo4j container and network were removed.

- remote_publication_marker_committed_before_source_deletion: pass
- hard_kill_after_neo4j_commit_leaves_durable_postgresql_retry: pass
- retention_sweep_finishes_receipt_idempotently_across_real_stores: pass

## Limits

- Source deletion is simulated in isolated PostgreSQL; the live API/R2/Qdrant path is not exercised.
- The hard kill occurs immediately after Neo4j tombstone commit, before PostgreSQL receipt commit; other crash boundaries remain untested.
- The live combined API schedules deleted-intent cleanup, but this run does not exercise its full R2/Qdrant deletion path.
- Automatic graph-only continuation remains disabled.

Source SHA-256 hashes and elapsed time are recorded in the adjacent JSON. Elapsed time is a test observation, not a capacity metric.
