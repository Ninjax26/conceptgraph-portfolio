# Generation-compatible graph projections — isolated Neo4j

Command: `.venv/bin/python scripts/evaluate_graph_baseline_projection.py`.

14 new ordering checks and 19 existing projection regressions passed. Legacy unfenced ordering safe: **False**. Live integration remains disabled.

- fenced_retry_preserves_new_graph_against_exact_older_marked_replay: passed
- older_and_same_generation_foreign_cleanup_cannot_erase_retry: passed
- baseline_replay_is_idempotent_but_payload_and_task_changes_are_rejected: passed
- baseline_can_advance_to_marked_publication_but_never_reverse_or_abort_it: passed
- attempt_cleanup_closes_late_writes_but_allows_a_new_generation: passed
- cleanup_before_first_projection_closes_the_failed_generation: passed
- permanent_deletion_rejects_even_higher_baseline_marked_and_cleanup_generations: passed
- new_baseline_waits_for_inflight_older_publication_then_wins: passed
- late_publication_waits_for_inflight_new_baseline_then_is_rejected: passed
- attempt_cleanup_waits_for_same_generation_baseline_then_closes_it: passed
- failed_baseline_rolls_back_projection_and_generation_together: passed
- failed_cleanup_rolls_back_concept_deletion_and_aborted_generation: passed
- empty_baseline_still_advances_fence_without_leaving_concepts: passed
- other_upload_and_other_course_survive_generation_writes_and_cleanup: passed

Limits:

- No PostgreSQL source/job authority, actual processor, Qdrant, R2 or HTTP API is exercised.
- Legacy ordinary writes/cleanup remain unchanged and unsafe in the separate replay probe.
- New checks use explicit source generations; live integration and cleanup qualification are still required.
- Outputs/failures are synthetic; no new hard-kill, capacity, semantic-quality or provider measurements.
- The existing projection regressions include real rollback, managed retry and cancellation checks.
- Automatic continuation remains disabled; this is not end-to-end retry or deletion acceptance.
