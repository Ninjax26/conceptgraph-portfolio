# Retry ordering and final-attempt recovery — isolated stores

Command: `.venv/bin/python scripts/evaluate_graph_retry_final_attempt.py --socket-dir <private-test-socket>`.

12 recovery/invariant checks passed. Ordinary retry ordering safe: **False**. Automatic dispatch remains disabled.

- postgresql_stale_claim_stays_fenced_after_ordinary_retry: passed
- last_attempt_crash_preserves_attempt_limit_and_remote_commit: passed
- read_only_final_attempt_recovery_finishes_once_without_extra_claim_or_remote_write: passed
- available_attempts_active_lease_and_other_stop_reasons_do_not_enter_receipt_recovery: passed
- pending_removal_does_not_complete_through_receipt_recovery: passed
- missing_remote_commit_never_grants_work_or_completes_exhausted_job: passed
- conflicting_remote_marker_cannot_complete_or_mutate_bookkeeping: passed
- changed_extraction_contract_fails_before_remote_observation: passed
- concurrent_expired_final_attempt_observers_finish_one_receipt_without_new_claim: passed
- bookkeeping_rollback_leaves_retryable_receipt_observation: passed
- repeated_cancellation_drains_receipt_commit_while_source_lock_fences_competitor: passed
- sigkill_after_read_only_completion_commit_is_idempotently_durable: passed

Activation blockers:

- An old marked replay can overwrite a newer ordinary unmarked retry projection.

Limits:

- Ordinary retry uses real upload lifecycle methods and unmarked graph methods, but not the live API, vector writes or full processor.
- Old remote work is adversarially replayed after the retry; this does not emulate packet delay or every server timing.
- The final attempt is reached by advancing the private fixture counter, not consuming provider attempts; its lease is explicitly expired.
- Recovery only observes exact existing markers; it must not extract, publish or grant another claim.
- No models, Qdrant, R2, hosted data, production test mutations or automatic dispatcher are exercised.
