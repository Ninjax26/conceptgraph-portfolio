# Graph publication recovery — local PostgreSQL and Neo4j

Command: `.venv/bin/python scripts/evaluate_graph_publication_recovery.py --socket-dir <private-test-socket>`.

- real_marked_publication_and_postgresql_completion: passed
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
- Existing unmarked ingestion/retry writes, superseded source generations and bounded dispatch still require combined verification before activation.
- Automatic graph continuation remains disconnected. Test elapsed time is not a throughput or capacity metric.
