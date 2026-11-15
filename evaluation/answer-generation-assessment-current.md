# Actual answer-generation evaluation

Completed 15 November 2026 using the approved bounded test. **11 real Groq outputs plus 1 separately labelled local no-source refusal** were captured. The request model was `openai/gpt-oss-20b`; source/prompt hashes match the approved manifest. No baseline output comparison was made.

| Observed measure | Actual result |
| --- | ---: |
| Hosted requests / approved maximum |11 /11|
| Retries / backup-provider calls |0 /0|
| Outputs finishing normally |11 /11|
| Largest completion / per-request cap |289 /1,200 tokens|
| Mechanically valid responses, including local refusal |12 /12|
| Valid source labels |17 /17|
| Wrong explicit cited pages |0|
| Literal supplied source passages |21 /21|
| Missing manufacturer/serial cases returning canonical model refusal |2 /2 known cases|
| Provider-reported input / output / total tokens |5,201 / 2,138 / 7,339|
| One-run SDK latency p50 / p95 |0.691s / 1.218s|
| Human-reviewed answers / semantic scores |0 / not measured|

The latency sample excludes 20-second spacing and is not end-to-end application or load-test latency. The no-source case did not consume a model request and must not be counted as a model refusal. Shared admission used the existing hook; shared database budgets were disabled in local settings, so the isolated runner enforced the approved count/output bounds. No budget setting was changed.

## What the answers revealed

The source labels are valid, but useful semantic problems remain. These are **assistant observations for human review**, not human-reviewed scores or a hallucination percentage:

- **qa01:** adds HTTP method/header/status examples absent from its supplied excerpt. Core fields are covered, but the extra examples violate a strictly source-bounded answer.
- **qa06:** adds overwrite/corruption consequences not explicitly given and strengthens a course prerequisite into “idempotency requires transactions.” Preserve source qualifiers.
- **qa07:** explains external Qdrant writes and exact-upload cleanup, but does not explicitly retain the expected cleanup-idempotency detail. Supplying oracle passages does not fix the existing q13 retrieval miss.
- **qa08:** includes external quota as a readiness dependency, losing the source caveat that a healthy process does not prove AI-provider quota.
- **qa04/qa05:** repeat their main distinction across multiple sections. Shorter direct answers would be easier to read.

No runtime/prompt correction was made after looking at these outputs, and no additional provider calls were made. This capture establishes a diagnostic baseline for those fixes, not a measured improvement.

## Next implementation priorities

1. Constrain optional examples and extra facts to details present in cited passages.
2. Preserve qualifiers such as “in this course/workflow” and readiness-versus-quota limitations.
3. Check requested-detail coverage and avoid repeated sections.
4. Keep work on missing retrieval evidence separate from generation: q13 still needs its required source.
5. Use the hash-bound human worksheet to review every claim/citation and expected detail; reserve a fresh heldout set before reporting generalization gains. Another live comparison requires a new explicit quota budget.

## Artifacts

- [Actual raw responses and exact source excerpts](answer-generation-current.responses.json)
- [Offline structural audit](answer-generation-current.md)
- [Unfilled human-review worksheet](answer-generation-current.review.json)
- [Execution metadata, per-case assistant notes, usage and hashes](answer-generation-assessment-current.json)
- [Approved request preflight](answer-generation-preflight.md)

The review worksheet is intentionally unfilled. Assistant judgments are stored separately and never entered as human review. The existing auditor also inspects an older graph snapshot; those graph counts are historical context, not new extraction results. All artifacts and existing Step 4 code remain local and uncommitted. No production PDFs, ingestion, storage, environment, credentials or deployment were changed.
