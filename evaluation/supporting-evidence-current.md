# Supporting evidence — 4 November 2026

The original question must already pass the existing evidence gate. One optional focused rerank can then add literal context from the same scoped candidates. Original citations stay first; total citations stay at most five; primary evidence score/level stay unchanged. The global threshold remains **0.35**.

| Source-selection check | Before | After |
| --- | ---: | ---: |
| Development + known validation: all required sources | 23/27 | 26/27 |
| Same set: unsupported refusals | 8/8 | 8/8 |
| Full original benchmark: all required sources | 31/35 | 34/35 |
| Full original benchmark: unsupported refusals | 10/10 | 10/10 |
| New reserved holdout: all required sources | 4/5 | 4/5 |
| New reserved holdout: unsupported refusals | 2/3 | 2/3 |
| Existing context development: all required sources | 3/3 | 3/3 |
| Existing context development: unsupported refusals | 1/2 | 1/2 |

**No improvement on unseen questions was demonstrated.** The development gain recovers q06 (TLS prerequisite), q16 (scoped vector search) and h10 (document authorization). q13 still loses its SQL-transactions source. These are local page-source coverage results, not generated-answer accuracy.

The new holdout was first measured only after freezing the reviewed rule; its baseline loaded trusted local Git code at b81befddfb4d19680b06bcf247fe5c7274323ff9. Its supported as01 question is still refused, and its absent-voltage as06 question still admits evidence. Existing ec04 manufacturer-missing evidence is also admitted. Do not tune against this consumed holdout and call it unseen validation.

## Safety and verification

- **701 backend tests pass** (final post-correction suite 7.998s), including 11 new supporting-evidence tests.
- **9 frontend tests pass**; `npm run build` passes TypeScript/Vite (Vite 17.06s).
- **58 exact checks each**: traced versus untraced output; primary citation prefix; primary refusal preservation; primary evidence level/score preservation. No pipeline errors in the 58 current model-evaluated cases.
- All 45 original refusal decisions and candidate recall are unchanged; no original source-completeness regression.
- Corpus/model/threshold equality, frozen rule/dataset hashes and unchanged original comparison parser AST verified.
- One fresh review found qualifier loss. Six RED negative subcases reproduced it; conditional, negated, contrastive and scoped qualifiers now retain the original path. No second review.
- `git diff --check` and Python AST/source-hash checks pass. Isolated dependency/resource warnings were observed. No dedicated backend type checker is configured.

## Changed files

- `app/services/evidence_facets.py`: one focused supporting query for explicit learn-before, still-need and same-scope alignment forms; conservative fallback.
- `app/services/rerank_service.py`: ordinary evidence must already pass and leave citation space. Optional extra scoring reuses scoped candidates, budgets and client cleanup; errors preserve the original ranking, cancellation propagates.
- `app/services/citation_service.py`: independently preserves primary refusal/citations, then applies existing evidence and identifier guards to supporting sources; prioritizes new pages.
- `app/services/evidence_trace.py` and `scripts/evaluate_retrieval_scaling.py`: distinguish original/supporting decisions and assert all preservation contracts.
- `tests/test_supporting_evidence.py`: failure, cancellation, identifier, cap, provenance and qualifier contracts.

## Operating cost and limits

An eligible non-comparison question may use **one additional rerank call**. Hosted Cohere therefore makes up to two sequential calls on that path. Full citation budgets, original refusals and unrecognized question forms skip the optional call. Every hosted call keeps existing shared request/token reservation and metrics; no hosted calls were made during this work.

Deferred minor: offline query/window counters describe successful returned work; failed optional attempts are omitted. They are not billed provider usage. Successful recorded evaluation runs had no optional errors. Legacy baseline-revision counters are not used for cost comparisons.

The focused check is still a relevance heuristic and can return merely topical context. Human claim/citation support, live-provider answer quality and production latency remain unmeasured. Only a small authored corpus was tested.

## Test progression

The focused `python -m unittest discover -s tests -p test_supporting_evidence.py`
first failed on the missing planner, optional-call behavior, refusal isolation and
confidence preservation, then passed after implementation. The later cap-trace
contract failed before its event was added. Six qualifier/scope subcases failed
in the final review correction, then all 11 test methods passed. Full-suite runs
passed at 697 tests, then 701 tests, with the final 701 run above authoritative.
These expected RED runs are regression evidence; no final failure is hidden.

## Reproduce

Use `<checkout>/.venv/bin/python`; local models must already be cached. Full original/context final runs used `/private/tmp`; the first reserved-holdout pair ran from the checkout. Both runners force local models and use owned authored PDFs/in-memory Qdrant.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<checkout> <checkout>/.venv/bin/python -m unittest discover -s <checkout>/tests
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 <checkout>/.venv/bin/python <checkout>/scripts/evaluate_retrieval_scaling.py --documents 10 --profiles identifier_10_verified --trace-evidence --output /private/tmp/cg-support-full-current
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 <checkout>/.venv/bin/python <checkout>/scripts/evaluate_retrieval_scaling.py --documents 10 --profiles identifier_10_verified --questions evaluation/questions-answer-selection-holdout-v1.json --baseline-revision b81befddfb4d19680b06bcf247fe5c7274323ff9 --output /private/tmp/cg-support-holdout-baseline
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 <checkout>/.venv/bin/python <checkout>/scripts/evaluate_retrieval_scaling.py --documents 10 --profiles identifier_10_verified --questions evaluation/questions-answer-selection-holdout-v1.json --trace-evidence --output /private/tmp/cg-support-holdout-current
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 <checkout>/.venv/bin/python <checkout>/scripts/evaluate_retrieval_scaling.py --documents 10 --profiles identifier_10_verified --questions evaluation/questions-evidence-context.json --splits development --trace-evidence --output /private/tmp/cg-support-context-dev-final
# From the checkout with the installed Node toolchain in PATH:
npm run test:frontend
npm run build
git diff --check
```

Rule/source/dataset hashes and compact source-loss decisions are saved in the JSON report; raw authored runs and their hashes remain in `/private/tmp`. The historic original baseline is the prior step1 artifact; archived source copies are in `/private/tmp/cg-step1-source-snapshot`.

## Next

Step3 should distinguish a topical match from support for the requested attribute and investigate remaining supported refusals/rollback evidence. q13/as01/as06/ec04 are now known regressions. Freeze a new holdout before choosing a correction, keep primary/scoping guards, and request a separate hosted quota budget before live-model evaluation.

All changes remain local on `codex/evidence-selection-tracing`. No commit, push, deployment, production data mutation, private-workspace feature or new dependency.
