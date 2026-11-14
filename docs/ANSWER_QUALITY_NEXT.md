# Next: recover useful evidence before changing answer generation

**4 November: step 2 is implemented locally.** Original source completeness
improves 31/35→34/35; development/known-validation 23/27→26/27. Original refusals
stay 10/10. The new heldout remains 4/5 completeness and 2/3 refusals: no unseen gain
was demonstrated. See [supporting-evidence report](../evaluation/supporting-evidence-current.md).
Primary citations, refusals and evidence level/score are preserved in 58 exact
model-evaluated checks. One optional extra rerank can add literal context to an
already-supported answer; global threshold stays 0.35. 701 backend and 9 frontend
tests plus TypeScript/Vite build pass. No deployment or hosted inference.

Next: step 3, distinguish topical relevance from the requested fact and investigate
remaining supported refusals. q13/as01/as06/ec04 are known regressions. The eight
holdout cases are NOW CONSUMED; freeze a different holdout before tuning to them.
Optional failed rerank attempt accounting in offline counters remains a deferred
minor. Live-provider answer quality and human citation support are unmeasured.

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

The saved observations above were reproduced in the 4 November offline trace.
All four sources reach the candidate pool, then their low local rerank scores
leave their evidence score below 0.35. This locates the rejection; it does not
prove lowering the global threshold is safe. Selection rules remain unchanged.

Three of those historical failures (q06/q16/h10) are now recovered locally; q13 remains.
Treat these known failures as regression/development examples. h10 was
historically labelled validation; do not present tuning on this known case as an
unseen holdout win. Author a separate held-out question set before changes, keep
unsupported missing-attribute questions, and measure evidence recall, source
completeness and refusals. Use cached local models and owned authored fixtures;
no hosted inference without separately approved quota. Human answer/citation/
relationship correctness remains unmeasured until actually reviewed.

Detailed current pipeline findings and execution criteria: [code audit](ANSWER_QUALITY_CODE_AUDIT.md).
