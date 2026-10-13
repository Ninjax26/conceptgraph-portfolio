# Graph checkpoint compatibility — offline checks

Generated: 2026-10-13T09:21:09.716484+00:00.

Command: `.venv/bin/python scripts/evaluate_graph_checkpoint_compatibility.py`.

20/20 contract checks passed. Hosted provider calls: **0**. Production test-data mutations: **0**.

| Check | Result |
| --- | --- |
| `test_cached_provenance_is_rebound_not_used_as_authority` | Pass |
| `test_compatible_serialized_checkpoint_avoids_model_call` | Pass |
| `test_configured_model_changes_invalidate` | Pass |
| `test_context_output_and_repair_changes_invalidate` | Pass |
| `test_corrupted_cached_graph_is_regenerated` | Pass |
| `test_explicit_empty_success_is_reused_but_implicit_empty_is_not` | Pass |
| `test_keys_are_stable_and_fit_existing_database_column` | Pass |
| `test_model_change_regenerates_instead_of_reusing_stale_checkpoint` | Pass |
| `test_prompt_schema_and_validator_version_changes_invalidate` | Pass |
| `test_quota_limited_output_is_not_a_completed_batch` | Pass |
| `test_quota_resume_calls_only_unfinished_compatible_batch` | Pass |
| `test_raw_unsupported_edges_and_missing_endpoints_are_rejected` | Pass |
| `test_recovery_key_includes_preceding_concept_names` | Pass |
| `test_recovery_recomputes_when_section_concepts_change` | Pass |
| `test_route_uses_same_primary_and_backup_precedence_as_generation` | Pass |
| `test_secret_rotation_quota_and_task_changes_do_not_invalidate` | Pass |
| `test_serialized_checkpoint_reuses_in_a_fresh_process` | Pass |
| `test_source_text_identity_and_provenance_changes_invalidate` | Pass |
| `test_unknown_cached_sources_are_regenerated` | Pass |
| `test_unversioned_checkpoint_is_retained_but_not_trusted` | Pass |

## Limits

- Model outputs are authored fixtures; no semantic correctness score or actual quota measurement.
- Fresh subprocess checks Python restart/serialization only, not split API/worker operation or hard-kill recovery.
- Per-upload checkpoints only; cross-upload caching and automatic graph-only continuation remain pending.
- Existing unversioned checkpoint rows are retained but not reused; a requested rebuild may need bounded fresh calls.

Runtime/test SHA-256 hashes are in the adjacent JSON report. These are structural checks, not human-reviewed graph accuracy.
