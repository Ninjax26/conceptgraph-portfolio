# Supporting Evidence Implementation Plan

> Execute inline with test-driven development and one fresh final read-only review.

**Goal:** Recover complementary source passages for questions already answerable,
without letting topical supporting matches unlock a refused answer.
**Spec:** docs/ANSWER_QUALITY_CODE_AUDIT.md step 2 and user “next continue”.
**Stack:** Existing Python RAG/rerank/citation services, cached MiniLM and Qdrant.

## Design and constraints

A local probe of literal one/two/three-sentence alternatives improved some logits
but none of the four missing required pages passed the unchanged .35 gate. Do not
ship more windows or lower the gate merely for that reason. Direct prerequisite
and literal subject/permission-focused probes recovered relevance for three
missing topics; PostgreSQL rollback remains a mismatch requiring later work.

Use a bounded supporting-query plan for explicit prerequisite learning questions,
“why does X still need Y”, and “why must X use the same ... as Y”. Preserve
qualifiers by falling back on ambiguous/compound questions. Original question
still governs retrieval, original scoring, final synthesis and identifier guards.

Only after ordinary prepare_evidence succeeds may reranking make one optional
supporting-query call on the SAME scoped candidates. Existing comparison facets
retain their exact flow. Catch optional request/scoring failures and return the
original ranking; propagate cancellation. Both hosted requests use the existing
request/token reservation, metrics and client lifecycle. No live hosted checks.

In final selection, independently rerun primary selection before considering
supporting chunks; refused primaries remain refused, original citations stay
first and unchanged, and total citations stay <=5. Supplemental literal passages
must still pass the same score/identifier guards. Prefer additional pages before
distinct windows on an existing page. Confidence level/score come from the primary
answer evidence. No document gets introduced from outside the original candidates.

## Tasks

1. RED→GREEN planner, primary refusal/identity/cap preservation, optional failure
   and cancellation, local/HTTP-mock orchestration contracts. Keep historical
   frozen-score regression observations; don't overwrite the step-1 baseline.
2. Implement focused query planning and bounded optional rerank; additive literal
   source selection. Extend offline trace role/plan accounting to match reality.
3. Compare development and existing regressions with saved baselines; fix rule
   before running frozen eight-case holdout. Do not tune after that run. Include
   existing missing-attribute weakness in reporting. No generated answer accuracy.
4. Full backend/frontend/build checks, one fresh review, one RED→GREEN correction
   pass for important findings. Report exact source/fragment/refusal results,
   provider-call increase and remaining misses. Keep all changes local.

## Review focus

- High topical support cannot unlock an unsupported or identifier-missing primary.
- Primary literal citations/IDs survive five-source saturation and deduplication.
- No cancellation swallowed or owned HTTP client leaked on optional failure.
- Ambiguous negation, qualifiers, compound topics and long inputs use full query.
- Trace roles distinguish original versus supporting scores for the same chunk.

## Ledger

- Baseline: step-1 690 backend tests; original local 31/35 completeness,10/10
  refusals; existing development challenge3/3 completeness,1/2 refusals.
- Ruling: continue on the existing isolated local branch, preserving all step-1
  work and historical reports. Current task is selection, not optional scaling.
- Ruling: no new approval needed for the already-agreed step-2 scope. The user
  asked to continue execution; no external writes, paid services or hosted quota.
- Pre-flight: rerank produces tagged scoped copies; citation consumes tags but
  must validate primary eligibility itself. Evaluator/trace must account for the
  optional query and actual roles, rather than treating it as a comparison facet.
- Eleven new contracts pass, including optional HTTP429 and cancellation, no
  model work when primary refuses or citations are full, and identifier-safe
  context. Final full suite before review correction:701 tests pass,18.547s;
  frontend9 and TypeScript/Vite build pass (Vite17.06s).
- Development/current historical validation:26/27 completeness vs23/27,8/8
  refusals unchanged; q06/q16/h10 recovered. Existing challenge development3/3,
  refusals1/2: manufacturer fact still absent but evidence admitted.
- One fresh reviewer found an important qualifier fallback gap. Conditional,
  contrastive, negated and prepositional-scope questions now stay on the original
  path. Six failing negative subcases reproduced the problem, then pass. One
  correction pass; no second review.
- Final minor (deferred): offline rerank counters reflect successful returned
  plans/windows; optional failed attempts are omitted. Successful evaluation
  runs have no optional failures, so their counts are valid for successful work,
  not billing/attempt accounting. Actual provider budgets/metrics are unchanged.
- Rule frozen after correction in /private/tmp/cg-support-rule-freeze.json.
  Reserved eight-case holdout has not been evaluated before this freeze. Next:
  baseline/current first holdout runs, final full corpus and artifact checks.
- Optional legacy-window experiment was not shipped: no missing source crossed
  the gate, so more candidate windows did not justify increased scoring work.

- Final post-correction suite:701 tests pass,7.998s. Focused11 tests pass with all
  qualifier subcases. Frontend9 and TypeScript/Vite build pass. Original full
  corpus34/35 completeness vs31/35;10/10 refusals unchanged. Development/known
  validation26/27 vs23/27;8/8 refusals. Existing challenge3/3 and1/2 unchanged.
- New heldout baseline and candidate both4/5 completeness,2/3 refusals. No gain
  demonstrated on unseen cases; source/rule hashes stayed frozen after observing
  results. Holdout now consumed; as01/as06 are future regressions, not tuning data
  for this change. Baseline legacy work counters omitted from comparison.
- All58 model cases pass exact trace-output, primary citation-prefix, refusal,
  evidence-level/score checks with no pipeline errors. Corpus/model/threshold
  match; original comparison parser AST unchanged. Frozen/source/artifact hashes
  and Python AST/diff checks verified.
- Task complete locally, not overall answer-quality completion. No remote changes,
  hosted-model semantic measurement or human review. Next is the refusal boundary,
  supported as01/q13 losses and missing attributes, with a new separate holdout.
