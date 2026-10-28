# Graph publication completion — isolated PostgreSQL

Command: `.venv/bin/python scripts/evaluate_graph_completion.py --socket-dir <private-test-socket>`.

- provider_limited_draft_cannot_complete_or_read_remote_marker: passed
- missing_marker_cannot_complete_or_change_ready_result: passed
- conflicting_marker_cannot_complete_or_change_ready_result: passed
- missing_draft_cannot_complete_or_read_remote_marker: passed
- missing_intent_cannot_complete_or_read_remote_marker: passed
- natural_lease_expiry_during_remote_read_cannot_stage_completion: passed
- stale_claim_cannot_read_marker_or_complete: passed
- expired_lease_fences_completion: passed
- pending_removal_fences_completion: passed
- staged_completion_keeps_source_lock_and_rolls_back_receipt_job_and_result: passed
- committed_completion_survives_restart_preserves_ready_identity_sources_and_checkpoints: passed
- completed_claim_cannot_complete_again: passed

Limits:

- Remote marker reads are authored doubles; no Neo4j write or cross-store atomicity is proven.
- No API/worker dispatcher, cancellation drain, killed publisher, hosted R2 or provider use is tested.
- Automatic graph-only continuation remains disconnected; this is caller-owned PostgreSQL finalization only.
