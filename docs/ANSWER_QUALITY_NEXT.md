# Next: recover useful evidence before changing answer generation

Backend safety remains in place. Do not add optional infrastructure features as
prerequisites for this work. Keep current API/auth/READY/citation/refusal behavior
unless a measured, tested evidence-selection correction justifies a change.

The saved `evaluation/evidence-facets-integration-original.json` identifies these
four unique supported-question misses in its first corpus/profile:

| Historic ID | Question | Saved stage | Required sources in candidate pool |
| --- | --- | --- | --- |
| q06 | What must I understand directly before HTTPS? | evidence_gate | yes |
| q13 | Why cannot a PostgreSQL rollback remove partial Qdrant writes? | evidence_gate | yes |
| q16 | Why must vector retrieval use the same document scope as access control? | evidence_gate | yes |
| h10 | Why does a signed login session still need authorization checks for each document? | evidence_gate | yes |

These are saved observations, not fresh measurements or proof that a threshold
is the cause. Reproduce current behavior and trace the actual selected windows,
reranking, passage selection and gate decisions before implementing a fix.

Treat all four now-known failures as regression/development examples. h10 was
historically labelled validation; do not present tuning on this known case as an
unseen holdout win. Author a separate held-out question set before changes, keep
unsupported missing-attribute questions, and measure evidence recall, source
completeness and refusals. Use cached local models and owned authored fixtures;
no hosted inference without separately approved quota. Human answer/citation/
relationship correctness remains unmeasured until actually reviewed.

Detailed current pipeline findings and execution criteria: [code audit](ANSWER_QUALITY_CODE_AUDIT.md).
