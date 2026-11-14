# Next: recover useful evidence before changing answer generation

**14 November: the scoped Step 3 fact guard is implemented locally.** For explicit
digit-bearing equipment IDs, manufacturer/voltage/serial evidence must contain
the requested positive fact in the final citation text. New frozen fact holdout
retains 5/5 supported cases and improves missing/wrong-property refusals 0/5→5/5;
development retains 5/5 and improves 0/4→4/4. Original 34/35 and 10/10 are unchanged.
726 backend tests, nine frontend tests and TS/Vite build pass. The new holdout is
now consumed. [Report](../evaluation/fact-availability-current.md).

**Next is Step 4: answer completeness and citation discipline.** Mechanically
validate generated citation indices/format, partial-evidence disclosures and
fallback coverage; then use separately approved provider captures and actual
human review to measure claim support and correctness. No hosted quota is
currently approved. Wider fact entailment, generic paraphrase recovery and
q13/as01 remain separate work. Do not treat this rule-based explicit-slot check
as an answer-accuracy model. One minor comparison witness-attribution issue and
failed-optional-work counters remain documented. The threshold remains 0.35,
five-citation cap and auth/READY/store behavior unchanged. Work is local only.

The dated material below records the earlier baseline and execution history.

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
