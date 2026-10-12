# Phase 1 — local integration

The measured comparison prototype is now part of `RerankService` and `prepare_evidence`, not a separate API mode. No deployment, database change or provider call was made during verification.

## Behavior

- Only simple, explicit **Compare/Contrast A with B** questions get two facet queries. Nested `with` comparisons and colon/semicolon/newline-qualified forms remain on the original full-question path. This is not general LLM-based question decomposition.
- The original READY-scoped candidate pool is reused. Each facet gets bounded literal 900-character windows and the unchanged **0.35** evidence threshold. No new vector or graph search is performed.
- Local scoring runs both facets in **one model batch**. Hosted Cohere uses **two sequential requests** for supported comparisons, instead of one. Other questions remain one request. More local inputs/hosted work are an explicit cost and latency tradeoff; hosted quality/cost was not measured.
- Requested identifiers are verified per facet. A missing side refuses before synthesis; a failure on the second hosted request does not return the first side as a complete result.
- Up to five sources must cover both facets and all requested identifiers. Literal duplicates are merged without losing their internal facet membership; source IDs are renumbered and internal tags are removed from the response.
- Comparison confidence uses the lower facet score and reports the actual final citation count. It remains a heuristic, not a calibrated probability that the answer is correct.
- Session verification, upload protection, READY filtering, graph modes, ingestion checkpoints, deletion, transactions, locks and storage behavior are unchanged.

## Measured results

| Set | Supported source completeness | Evidence-gate refusals |
| --- | ---: | ---: |
| Original benchmark, 10/50/100 PDFs | 31/35 (unchanged) | 10/10 (unchanged) |
| Comparison development questions | 3/3, previously 2/3 | 1/2 (unchanged) |
| Fresh authored integration holdout, 10/50/100 PDFs | 4/4 | 2/2 |

The fresh six-question holdout was authored before its first measured run and was not used to choose thresholds or tune the integrated policy. It is authored educational data, not independently annotated customer traffic. Its unsupported cases test a missing comparison side/identifier; **known-identifier missing attributes remain unresolved**. The four original weak-source misses also remain.

Reports: [original benchmark](evidence-facets-integration-original.json), [development](evidence-facets-integrated-development.json), [fresh holdout](evidence-facets-integration-heldout.json). Reported hashes identify datasets, fixture bytes and the implementation. Local runs use cached MiniLM, in-memory Qdrant and no answer generation. They do not establish production performance or hosted-model accuracy. Final corpus-size runs ran concurrently, so do not claim a latency speedup from them.

## Verification

Ten additional boundary tests exercise local batching, hosted request counts, owned/borrowed client lifecycle, second-facet failure, per-facet identifiers, shared citation limits, deduplication, conservative parsing and synthesis prevention at the API boundary. Existing retrieval and processing tests remain in the full suite.

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python scripts/evaluate_retrieval_scaling.py --documents 10 50 100 --profiles identifier_10_verified --output evaluation/evidence-facets-integration-original
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python scripts/evaluate_retrieval_scaling.py --documents 10 50 100 --profiles identifier_10_verified --questions evaluation/questions-comparison-integration-heldout.json --output evaluation/evidence-facets-integration-heldout
.venv/bin/python -m unittest discover -s tests -q
```

Hosted behavior is verified with HTTP mocks, not paid/billed live requests. These measurements were made locally before release; subsequent deployment verification is recorded separately in the [implementation ledger](../docs/IMPLEMENTATION_PROGRESS.md).
