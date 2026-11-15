# Next: recover useful evidence before changing answer generation

**15 November: current-prompt comparison preparation.** Released runtime baseline
is `d5626c2`. Added `scripts/prepare_answer_comparison.py`: a network-free builder
with exact known-context preservation, literal source bounds, current request
hashes, checklist/old-answer exclusion and exclusive output creation. The
[prepared pack](../evaluation/answer-comparison-preflight-v1.md) proposes 11 known
comparison requests plus 8 fresh candidate-only requests, each capped at 1,200
completion tokens, zero retries/failover. No model requests, answers or semantic
scores were produced. The earlier budget is exhausted; execution needs new
explicit approval. Earlier local/uncommitted status entries are historical.


**15 November: the offline source-faithful prompt correction is complete.**
The prompt now explicitly restricts examples/background additions, preserves
qualifiers and conditions, retains material requested details and discourages
repeated sections. Refusal applies when none of the requested parts can be
answered; partial answers still state missing details. This is model guidance,
not a new semantic output filter or demonstrated generation-quality gain.

Provider admission counts actual assembled prompt text, including Gemini's
separator, via instance dispatch. All five existing provider paths are covered.
The offline evaluator now accepts `--cases` with schema/source/unique-claim-ID
checks and preserves default cases and existing review files. 765 backend tests
pass, including 16 new methods and an actual mocked Gemini payload check. The
existing nine frontend tests and TS/Vite build passed during this increment;
frontend source did not change in the final backend correction. [Report](../evaluation/source-faithful-synthesis-current.md).

**Next is a separately approved candidate-generation comparison.** Eight fresh
quality questions were frozen before these edits and remain ungenerated/unscored.
Use a new prompt-bound manifest and the `--cases` evaluator path; do not reuse the
consumed 11-request budget or its old captured outputs as new-prompt evidence.
Human semantic reviews remain zero. Stronger guidance grows the known eleven
Groq-context prompt text from 22,063 to 33,690 characters; exact accounting is now
correct, but live quality, latency and token/cost effects are unmeasured. The old
11 model outputs and reports are unchanged. No hosted calls, release or production
mutation occurred in this increment. Wider fact entailment and q13/as01 retrieval
remain separate work. All current implementation changes are local/uncommitted.

Earlier dated entries retain their historical scope.

**15 November: the explicitly approved 11-request Groq capture is complete.**
All 11 requests used the approved oracle-source payloads with 1,200 completion-token
caps, no retries or backup calls. All ended normally; total reported usage was
5,201 input + 2,138 output = 7,339 tokens. One no-source refusal was recorded locally,
so the audit contains 12 responses but only 11 model generations. Mechanical
checks: 12/12 contracts, 17 valid labels, zero invalid indices/pages and 21 literal
source passages. Human semantic reviews remain zero. [Actual assessment](../evaluation/answer-generation-assessment-current.md).

**Next: source-faithful, concise answers and qualifier/detail preservation.**
Assistant analysis (not human scores) identified unsupported HTTP examples in
qa01, overgeneralized prerequisite language in qa06, a missing explicit cleanup-
idempotency detail in qa07, and readiness/quota conflation in qa08. qa04/qa05 repeat
points. Preserve the current frozen capture as diagnostic baseline; do not call
these results a measured semantic improvement or an unseen holdout. The q13
retrieval source miss remains separate despite its oracle synthesis answer.

All 11 approved requests have been used; there is no remaining hosted budget under
that approval. Make any next prompt/contract correction offline, reserve a fresh
heldout quality set, and obtain a separate bounded budget for a live comparison.
Use the existing blank hash-bound worksheet for actual human claim/citation/
completeness review. No runtime/prompt edits, production data mutations or release
were made during capture. Local shared DB budgets were disabled in configuration;
the runner used the existing admission hook and its own hard caps/spacing, so this
is not shared-quota qualification. Existing uncommitted Step 4 code is unchanged.

Earlier dated entries retain their historical preparation/pending scope.

**15 November follow-up: the real-generation evaluation pack is prepared offline.**
[Approval preflight](../evaluation/answer-generation-preflight.md) contains the
exact current local prompt/payload and 21 literal authored excerpts for 12 known
quality cases. Proposed execution: at most 11 Groq requests, 1,200 completion
tokens each (13,200 total), no retries or backup providers, stopping on a quota or
provider failure. The no-source case is a local refusal with no provider call.
The pack uses oracle sources to isolate synthesis and does not test retrieval or
unseen generalization. Rough input estimate is 5,521 tokens from 22,063 prompt
characters; this is not exact billing or a monetary price.

No provider call or human review has occurred. The earlier explicit offline-only
instruction still applies. A separate bounded quota approval has been requested;
preparation is complete and hosted execution remains pending that answer. There
is no capture runner or active enforcement change in this preparation increment.
Prior Step 4 runtime/test hashes are unchanged; earlier heldout remains consumed.

**15 November: Step 4's mechanical safeguards are implemented locally.** Provider
answers now need valid source/page references and usable text, with canonical
refusal allowed. Existing failover handles invalid outputs; fallback preserves up
to five accepted excerpts at the existing 900-character bound. It labels partial
excerpts and escapes Markdown/entity text. The prompt asks for direct, cited,
complete-as-supported answers and explicit missing details.

Frozen format holdout: valid 3/3 retained, invalid rejection 0/7→7/7. Development:
valid 4/4 retained, invalid rejection 1/12→12/12. 749 backend tests, nine frontend
tests, TS/Vite build and seven actual renderer assertions pass. Existing offline
fallback capture: 12 responses, 21 valid references, 21 literal source passages,
zero hosted calls/human reviews. [Report](../evaluation/answer-contract-current.md).
This is reference/format validation and fallback evidence coverage, not semantic
answer accuracy. The new heldout is now consumed. Changes remain uncommitted and
undeployed; last verified release is `8de7beb` from 14 November.

**Next: measure actual generated answers and citation support.** Collect a small,
separately budgeted provider capture only after explicit quota approval, then use
real human review for claim support, missing requested details and refusal
correctness. Existing hash-bound worksheets are available. Prompt improvements
alone do not establish better answers, and a valid citation index does not prove
its claim. No hosted quota is approved for this increment. Wider fact entailment,
supported paraphrase misses including q13/as01, full Markdown-context evaluation
and provider-specific quality remain open. Avoid optional infrastructure work as
a prerequisite for this answer-quality evaluation.

Earlier dated checkpoints below retain their own historical scope.

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
