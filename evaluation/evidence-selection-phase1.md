# Phase 1 — Evidence selection

## Scope and result

Continuation: [remaining-miss audit and citation-selection fixes](evidence-selection-followup-notes.md). The original reports below are retained; the final follow-up also records 31/35 strict source completeness and 10/10 unsupported refusals, without claiming an additional aggregate gain.

Compared the local reranker/evidence implementation at commit `9d830b3` with the working-tree changes, using the unchanged `identifier_10_verified` profile, ten retrieval candidates, and evidence threshold **0.35**. No threshold was lowered, model changed, LLM called, or production data modified.

Across each of the 10-, 50-, and 100-PDF corpora:

| Split | Required-source completeness before | After | Unsupported refusals before → after |
| --- | ---: | ---: | ---: |
| Development | 12/17 | 14/17 | 5/5 → 5/5 |
| Diagnostic validation | 9/10 | 9/10 | 3/3 → 3/3 |
| Previously frozen authored held-out | 8/8 | 8/8 | 2/2 → 2/2 |
| Total | 29/35 | 31/35 | 10/10 → 10/10 |

The improvement is **two development cases**, not a measured held-out gain. The existing held-out questions were not used to choose the window policy or threshold. They are previously authored fixtures, not new independently annotated customer questions. Every labelled passage fragment in this dataset remained present in the final evidence.

## Investigation of the six original misses

All six required pages reached the reranked top five. None was a search/candidate-pool or top-five ranking miss. Required pages were lost at the evidence gate. Inspect per-candidate scores and literal excerpts in the baseline JSON.

| Case | Missing evidence / cause | Outcome |
| --- | --- | --- |
| `q03` — HTTPS foundational prerequisites | The TLS page mixed explanation and prerequisites; its full-passage logit was -2.737, below the combined evidence threshold. | Recovered using a literal prerequisite-focused window. |
| `q06` — direct HTTPS prerequisite | TLS explanation remains weakly scored for this wording (baseline logit -6.104). | Still incomplete; no forced admission. |
| `q13` — PostgreSQL rollback versus Qdrant writes | The general transactions page remains weakly scored (-7.022); the explicit compensating-cleanup page is accepted. | Still incomplete under the two-page label. |
| `q16` — retrieval scope versus access control | The general vector-search page remains weakly scored (-6.761); the access-control page is accepted. | Still incomplete under the two-page label. |
| `q17` — readiness versus HTTP 429 | Identifier filtering incorrectly excluded the readiness page; the status-code page's whole-passage score was also too low (-1.471). | Both pages recovered: complementary context plus an identifier-focused status-code window. |
| `h10` — session versus document authorization | The document-access-control page remains weakly scored (-2.852); authentication and authorization pages are accepted. | Still incomplete under the three-page label. |

Some expected pages repeat facts already present in accepted pages. Failure to return every labelled page does **not** automatically mean an answer is wrong. This check measures labelled source coverage, not generated-answer correctness.

## Implementation

- `retrieval_candidates.rerank_passages`: retain the original bounded passage and add at most two literal, overlapping sentence windows with meaningful question/identifier matches. Do not spend additional window work on passages naming only other identifiers. Decimal values remain intact.
- `RerankService`: score bounded windows and map the best result back to its original chunk. Local inference is batched; hosted Cohere still uses one HTTP request. Original text, document scope, page metadata, score conversion, and invalid/incomplete-response checks are preserved.
- `citation_service`: cite the actual scored window only if it is literal source text. Complementary passages may omit a named identifier, but still must pass the unchanged threshold. Passages naming only conflicting identifiers are excluded. Every requested identifier must remain verified in accepted evidence **and final citations**.
- The runner now supports split/profile selection, comparison with trusted local Git modules without modifying the checkout, source-code hashes, per-candidate diagnostic scores, window counts, and separate split summaries. Generated PDF trailer IDs are deterministic so corpus byte hashes match between runs. Diagnostics contain only authored local fixture content; API logging is unchanged.

## Tradeoff and remaining work

Shorter scoring windows add reranker inputs; at most three are scored per candidate. This does not add generation calls or increase the dense shortlist, but hosted rerank billing/work may increase. Local timing measurements are available in both JSON reports; they are not hosted latency/cost predictions or a general speedup claim.

With the final policy, the mean number of rerank inputs was **14.02, 14.16, and 14.16** for the 10-, 50-, and 100-PDF corpora, respectively, versus ten baseline inputs. Mean local rerank times were **102.60, 114.49, and 85.73 ms** (baseline: 73.14, 80.14, 97.76 ms). Single-run timings fluctuate; the lower time at 100 PDFs is not evidence of a speedup. The extra work is explicit: this is a quality improvement, not a speed optimization. There is no extra hosted HTTP request, but more input passages may affect provider charges.

Four original cases remain incomplete. Preserve their regressions and investigate semantic question decomposition or complementary-evidence scoring separately, using development data. Do not weaken the gate merely to obtain 35/35. Held-out coverage did not improve in this experiment.

## Reproduction

Use cached models; both commands avoid provider requests, remote stores and production uploads:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python scripts/evaluate_retrieval_scaling.py --documents 10 50 100 --profiles identifier_10_verified --baseline-revision 9d830b3 --output evaluation/evidence-selection-baseline
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python scripts/evaluate_retrieval_scaling.py --documents 10 50 100 --profiles identifier_10_verified --output evaluation/evidence-selection-current
.venv/bin/python -m unittest discover -s tests -q
npm run test:frontend
npm run build
```

Use `--splits development validation` while developing a change, then freeze the implementation before evaluating held-out cases. The existing PDFs, labels, candidate budget and evidence thresholds were not changed.

Limits: in-memory Qdrant brute-force search, cached local MiniLM models, authored PDFs, one run per question/profile/corpus, no live Cohere calibration, no generated-answer evaluation, no production load or graph-mode comparison. Results must not be presented as customer-data accuracy or proof of production capacity.

## Verification

- Backend: **321 tests passed**, including 13 new evidence-selection boundary tests and one deterministic-fixture regression.
- Frontend: **9 tests passed**; TypeScript and Vite production build passed.
- Python compilation and `git diff --check` passed.
- Baseline and updated local runs each exercised 45 questions across three corpus sizes with no request errors. Corpus byte hashes, dataset labels and thresholds matched exactly; the updated report records matching code hashes.
- Hosted Cohere mapping and failure handling were checked with deterministic HTTP mocks, not billed live requests. No deployment or push was performed for Phase 1.
