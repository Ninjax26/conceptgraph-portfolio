# Answer quality: code audit and execution order

**16 November: citation compatibility and source-condition improvements.** Exact
complete `【Source N】` labels now become standard citations before validation;
wrong indices/pages, malformed labels and empty cited passages still fail. A
valid normalized primary answer avoids an unnecessary backup call. Fallback
excerpts keep typographic source text literal in the installed Markdown renderer.

The prompt now highlights bounded, literal scope/negation/condition sentences
and asks the model to check each dependency or guarantee. A reviewed clipping
fix excludes unterminated fragments even after retrieval has shortened a source.
All 781 backend tests pass, including 11 new methods. Nine frontend tests, the
production build and four renderer checks also passed during this increment.
The consumed-output format replay improves 17/20→20/20, not semantic accuracy.
The known qa06 semantic failure is not repaired by formatting normalization.
[Report and prompt-size tradeoff](../evaluation/citation-scope-revision-current.md).

No new hosted call or human review was made. Highlighting is lexical guidance;
model adherence and claim support remain unmeasured. Full original evidence is
still supplied, including complete sentences without terminal punctuation that
may not receive highlights. Both prior quota allowances are exhausted. Current
changes are prepared for a local commit; deployment is a separate action.


15 November offline correction: generic synthesis rules now explicitly preserve
source qualifiers and material detail, avoid unprovided examples and repetition,
and cite claims nearby. Canonical refusal applies only when no requested part is
supported. No lexical semantic veto or additional generation request is added.
Actual system/user prompt text is counted for existing admission, including
Gemini join separators and instance dispatch. The optional offline `--cases`
path validates case/claim schemas and unique claim IDs so malformed checklists
cannot silently inflate CLI completeness. Defaults/review preservation are tested.
765 backend tests pass; old captured answers/evidence/fact/rerank source hashes
remain preserved. Eight new quality questions are frozen but unrun. Prompt
adherence, human semantic quality and new token/latency effects are still
unmeasured; the longer prompt must be compared under a fresh explicit quota budget.
[Exact results and limits](../evaluation/source-faithful-synthesis-current.md).
No commit or deployment occurred.

15 November actual-output update: an explicitly approved bounded Groq capture
now provides 11 model outputs plus one labelled local refusal from oracle source
context. No prompt/runtime changes were made during capture. All 12 responses
pass mechanical contracts; 17 source labels and 21 supplied excerpts are valid.
Assistant analysis still finds unsupported examples and stronger-than-source
qualifiers, omitted explicit cleanup idempotency and readiness/quota conflation.
This demonstrates why format validation is not semantic claim support. Human
semantic metrics remain unmeasured; no generalization/improvement score is claimed.
[Actual findings and exact usage](../evaluation/answer-generation-assessment-current.md).
Prioritize source-faithful examples, qualifiers, requested-detail coverage and
conciseness. Retrieval and q13 remain separate. All 11 approved requests are used;
further hosted evaluation needs a new explicit budget. No release occurred.

15 November update: Step 4 adds a shared pure answer/reference contract to all
provider synthesis call sites and the offline auditor. It rejects invalid source
numbers/pages, missing or unfinished labels, source-definition-only output,
citation-only text and question echoes plus citations. Canonical refusal remains
allowed without citations. Invalid output reuses existing provider failover;
source-safe errors, quota observation, cancellation and store behavior remain.
Fallback now includes up to five 900-character accepted excerpts with original
indices, explicit limitations and escaped Markdown/entities. Prompt completeness
instructions are unmeasured on real generations. Frozen heldout format rejection
improves 0/7→7/7 with 3/3 valid retained; 749 backend tests pass. There are no new
semantic human scores, hosted calls, API/schema/auth/store changes or deployment.
[Results and limits](../evaluation/answer-contract-current.md). Next is separately
budgeted generation capture and human claim/citation/completeness review. The
older dated pipeline audit below is historical, including its two-source fallback.

14 November update: the explicit named-equipment manufacturer/voltage/serial
fact gate now runs on final literal citations before synthesis. Missing facts
cannot be replaced by topical relevance, brand labels or the wrong voltage role.
Bounded fact witness selection reuses the existing five-source selector and
1,024-state constraint; no model call, API, provider or store change. New heldout
5/5 supported and 5/5 refusals vs baseline 5/5 and 0/5; original source coverage
remains 34/35 and refusals 10/10. 726 backend tests pass. This is narrow mechanical
fact availability, not universal entailment or generated-answer correctness.
[Results and limits](../evaluation/fact-availability-current.md). Next is Step 4;
remaining q13/as01 recovery and broader scoring calibration are still unverified.
All changes for this increment remain local, uncommitted and undeployed. Earlier
dated audit text below describes its own prior pipeline state.

4 November update: this audit records the pre-step-2 pipeline. Evidence tracing
and guarded supporting context are now implemented locally; see the
[current measurements](../evaluation/supporting-evidence-current.md). Original
source completeness is 34/35 vs 31/35, with no unseen holdout gain demonstrated.
Step 3 refusal/answerability and remaining supported misses are next. Existing
APIs, READY/auth/store safety remain unchanged; live answer accuracy is unmeasured.

Audited 3 November 2026. This is a source-grounded implementation plan, not a
claim of improved answer accuracy. Existing backend qualification stays intact.

## Current request path

`app/api/endpoints/query.py` resolves eligible READY documents, calls retrieval,
reranks candidates, calls `prepare_evidence`, then synthesizes only if sources
survive. A graph-unavailable fallback uses vector retrieval. Query-specific graph
context reaches synthesis only for `filter_reason=query_subgraph`.

`app/services/rag_service.py` retrieves a default ten candidates using dense
search over the question expanded with graph prerequisites. An additional bounded
Qdrant payload lookup finds normalized identifiers, and reciprocal-rank fusion
combines them. This is already dense plus exact identifier retrieval, not a
complete BM25/full-text hybrid search. READY/upload/task scoping must survive all
changes.

`retrieval_candidates.py` uses question-focused literal excerpts up to900 chars
and at most two additional two-sentence windows. `RerankService._rank_windows`
retains only the highest-scoring window per chunk. Cohere relevance is converted
to logit, then citation scoring converts it back with sigmoid; local reranking
also uses sigmoid over its model scores. These values are not calibrated answer
correctness probabilities, and providers need independent evaluation.

`citation_service.py` accepts a weighted score (0.7 rerank +0.3 dense, minimum
0.35), checks identifiers, deduplicates and caps cited passages at five. Coverage
selection reserves identifiers and, for supported explicit Compare/Contrast
questions, both facets. It does not generally enforce coverage of every requested
attribute, cause, prerequisite or list item. Accepted full-chunk identifier checks
are followed by final cited-excerpt checks, which are deliberately conservative.

`synthesis_service.py` prompts for evidence-only answers and readable citations.
Runtime validation currently rejects empty output and pure question echoes; it
does not establish that every claim is supported or every cited index is valid.
The clearly labelled provider-unavailable fallback quotes only the first two
sources and truncates each to600 chars. That can be incomplete even when more
accepted sources exist. The offline quality evaluator can check citation format
and source literals, with human-review worksheets for semantic support.

## Measured starting point and limits

The saved integration benchmark retains31/35 required source sets and refuses
10/10 unsupported benchmark questions. q06/q13/q16/h10 have their required sources
in the candidate pool but lose required evidence before final acceptance. The
saved `evidence_gate` stage does not distinguish bad window choice, low reranker
scores, identifier rejection or final citation selection. Reproduce before fixing.
Historic h10 is a known validation failure; tuning against it is regression work,
not an unseen holdout success. Missing attributes of known identifiers are a
separate documented refusal weakness. Local and hosted rerank paths are not
interchangeable accuracy measurements; new hosted tests require approved quota.

## Execution order

1. **Trace selection, without changing answers.** Extend the offline evaluator to
   record candidate provenance, each literal window, rerank score, evidence-score
   components, rejection reason and final-source removal. No private source text
   in production logs. Capture the four regressions plus unsupported attribute
   cases, and author a separate holdout before changing selection rules.
   Acceptance: every missing required source has an attributable loss stage and
   reproducible local test. Use identifiers/source positions in public reports.

2. **Recover complementary evidence.** Test keeping multiple useful windows from
   a chunk and limited neighboring context when provenance/page remain known.
   Keep literal citations and bounded context; compare against the current single
   best window. Test cause/effect, prerequisites, lists and multi-document coverage.
   Do not lower the global threshold as a substitute for understanding a failure.
   Acceptance: improved development source completeness, reported untouched
   holdout results and no regression in supported unsupported-question refusals.

3. **Measure the refusal boundary and scoring.** Separate source relevance from
   whether the requested attribute is actually present. Characterize lexical-only
   candidates (their dense score can be absent) and each rerank provider's score
   handling. Tune only on development data; preserve source and identifier guards.
   Acceptance: explicit missing-attribute cases refuse while supported paraphrases
   survive. Report precision/recall tradeoffs, not an uncalibrated confidence rate.

4. **Improve answer completeness and citation discipline.** Once evidence is
   reliable, test a concise direct answer followed by requested details and cited
   limitations. Validate citation indices/format mechanically; test empty/echo,
   invalid references, partial evidence and fallback completeness. A valid citation
   index is not proof of claim support. Use hash-bound human review for correctness,
   completeness and claim-level citation support. New provider captures remain
   separately budgeted; no hallucination percentage without actual review.

5. **Add broader search only if traces show search failures.** The current
   identifier regex mainly recognizes digit-bearing IDs/specifications; pure-letter
   acronyms such as SQAP are not reliably covered by that lookup. Evaluate acronym
   normalization or full-text/BM25 candidates on unseen questions if candidate
   recall is lacking. Preserve scope, deduplication and separate ranking signals.
   Do not add another database or a larger model by default.

## How to judge improvement

For each stage compare a versioned baseline and development/held-out results:
required-source completeness/recall, MRR or nDCG for ranking, unsupported refusal
precision/recall, human answer correctness/completeness and citation support,
latency and real-versus-estimated provider usage. Existing 10/50/100-PDF authored
fixtures measure their own retrieval workload, not production capacity. Existing
human semantic scores remain unmeasured until reviewed.

First implementation task: stage-level evidence-selection tracing and regression
fixtures. Chunking overhaul, global threshold changes, stronger generation models
and broad hybrid search wait for evidence that they address the measured loss.
