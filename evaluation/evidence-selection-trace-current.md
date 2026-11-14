# Evidence selection trace — 4 November 2026

Historical step-1 baseline. Its code hashes and 31/35 result describe the earlier
snapshot; supporting-evidence changes are measured in
[the step-2 report](supporting-evidence-current.md).

Step 1 adds diagnostics only. Search/rerank inference and source selection remain unchanged.

| Offline check | Before | Traced |
| --- | ---: | ---: |
| Supported questions retaining every required source | 31/35 | 31/35 |
| Original unsupported questions refused | 10/10 | 10/10 |
| Existing context development source completeness | — | 3/3 |
| Existing context development unsupported refusals | — | 1/2 |

All 45 original result rows match baseline after excluding timings and the added trace diagnostics. Exact source objects and evidence assessments match the untraced call on the same ranked inputs for all 50 evaluated questions. Threshold stays **0.35**.

## Four confirmed source losses

| ID | Missing authored source | Vector contribution | Rerank contribution | Evidence score | Actual rejection |
| --- | --- | ---: | ---: | ---: | --- |
| q06 | web-foundations.pdf p2 | 0.1736 | 0.0016 | 0.1751 | below_threshold |
| q13 | data-reliability.pdf p1 | 0.0972 | 0.0006 | 0.0978 | below_threshold |
| q16 | data-reliability.pdf p5 | 0.1179 | 0.0008 | 0.1187 | below_threshold |
| h10 | identity-security.pdf p3 | 0.1490 | 0.0382 | 0.1872 | below_threshold |

Each required source reaches search and the candidate pool. The local reranker selects the full short source passage; its low score leaves the weighted evidence score below 0.35. Trace events link the actual selected/rejected windows and gates to source hashes and normalized character positions. These observations identify the loss; they do not justify lowering a global threshold.

The existing development question **ec04: “Who manufactured PMP-104?”** admits evidence despite an absent manufacturer attribute. ec05 refuses. This pre-existing weakness is reported separately from the original 10/10 refusal result. No answer is generated in these checks.

The four known misses, including historical validation h10, are development regressions. A new authored eight-case holdout is frozen and **unrun**. No independent human answer/citation review is claimed.

## Implementation

- `citation_service.py`: optional decision hooks at existing gates, identifier coverage, deduplication, citation cap and comparison selection; no HTTP response changes.
- `evidence_trace.py`: caller-owned hashed events; never automatically logged or returned by the API.
- `evidence_selection_trace.py`: offline adapters observe actual Qdrant results and every scored rerank window without extra model/search requests.
- `evaluate_retrieval_scaling.py --trace-evidence`: source-level loss reasons and exact traced/untraced source/confidence assertion. Legacy baseline mode refuses tracing because its modules lack hooks.
- Thirteen tests include four frozen-score regression replays; these exercise real window selection/gates without pretending to re-evaluate the neural model.

## Verification commands

Run from `/private/tmp` with the project root in PYTHONPATH and no project environment file; evaluator entry paths below resolve against the checkout. Use its `.venv/bin/python`.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<checkout> <checkout>/.venv/bin/python -m unittest discover -s <checkout>/tests
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 <checkout>/.venv/bin/python <checkout>/scripts/evaluate_retrieval_scaling.py --documents 10 --profiles identifier_10_verified --output /private/tmp/cg-evidence-baseline
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 <checkout>/.venv/bin/python <checkout>/scripts/evaluate_retrieval_scaling.py --documents 10 --profiles identifier_10_verified --trace-evidence --output /private/tmp/cg-evidence-traced-final
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 <checkout>/.venv/bin/python <checkout>/scripts/evaluate_retrieval_scaling.py --documents 10 --profiles identifier_10_verified --trace-evidence --questions evaluation/questions-evidence-context.json --splits development --output /private/tmp/cg-evidence-context-traced
```

The recorded baseline ran before the hooks. Baseline reconstruction can use `--baseline-revision b81befddfb4d19680b06bcf247fe5c7274323ff9` without tracing. Both final evaluator runs used `/private/tmp`; they matched the original baseline outcomes and all same-input untraced checks. Models are forced local and loaded with `local_files_only=True`.

- Backend: **690 pass**, 2.745s; initial baseline 677 pass.
- Frontend: `npm run test:frontend` — **9 pass**.
- `npm run build` — TypeScript/Vite **pass**, Vite 4.55s.
- `git diff --check` — pass.
- Full 45-case and five-case development evaluations — no pipeline errors. Metrics and limitations are in the JSON report.
- Dependency deprecation/resource warnings occurred in isolated tests; no hosted/provider or production data requests were made.

## Next change

Use these regressions to test question-focused supporting context and complementary evidence before adjusting the refusal boundary. Keep literal page provenance and strict document scope. Develop against known cases, then run the reserved holdout once the rule is fixed. Do not optimize against holdout outcomes. Hosted reranker quality and human answer correctness still require separate verification.

Changes remain on a local branch. No commit, push, deployment or production feature activation.
