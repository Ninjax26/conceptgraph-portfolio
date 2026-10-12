# Phase 1 follow-up — remaining misses and citation selection

Latest continuation: [scored-context fixes and a harder challenge set](evidence-selection-context-notes.md). The measurements below describe the preceding implementation and are retained for comparison.

## What changed

This is a local continuation of [the first Phase 1 experiment](evidence-selection-phase1.md), not a deployment or a new accuracy claim.

Two failing regression cases were reproduced before changing the code:

1. **Citation-budget starvation:** five accepted passages about PMP-104 filled the citation budget, excluding a sixth accepted passage about PMP-106. The final identifier check then refused an answer despite sufficient evidence for both documents. Selection now reserves a covering set of already-accepted, literal citations before filling remaining slots by rank. It keeps the original top five unchanged when they already cover all requested IDs. The five-source limit, evidence threshold and final identifier verification remain unchanged. If coverage cannot fit, the request still refuses.
2. **Conflicting selected excerpt:** a chunk mentioned PMP-104 in its introduction, but its selected rerank window described only PMP-105. The earlier filter checked the whole chunk and could admit that conflicting excerpt alongside another valid PMP-104 source. The filter now checks the same validated literal passage used for citation. Nonliteral window metadata falls back to the source text; it cannot introduce evidence.

Coverage selection uses at most eight identifier bits (256 states), not a model or an external service. Rank breaks ties between covering sets. Final sources retain ranked order and contiguous source IDs. No arbitrary document-diversity quota is used: a passage must still pass the evidence gate.

The local evaluation runner also reports `source_loss_diagnostics`, distinguishing absence from search, reranking, the evidence gate, and final citation selection. This examines the actual candidate lists, independently of the existing top-five ranking metrics. It does not add production logging of PDFs or questions.

## Remaining four source-label misses

All four missing pages were retrieved and reranked. All were rejected by the unchanged **0.35** evidence gate in the final runs.

| Case | Missing labelled page | Useful evidence already retained | Interpretation |
| --- | --- | --- | --- |
| `q06`: direct prerequisite for HTTPS | web-foundations.pdf, page 2 (TLS) | Page 3 explicitly says to study TLS before HTTPS and HTTP before TLS. | The extra TLS page repeats prerequisite information. Required-page completeness still fails; generated-answer correctness was not tested. |
| `q13`: PostgreSQL rollback versus Qdrant writes | data-reliability.pdf, page 1 (Transactions) | Page 4 explicitly says a PostgreSQL rollback cannot erase Qdrant writes and describes compensating cleanup. | The broad transaction page is weakly scored even though related. No forced admission. |
| `q16`: retrieval scope versus access control | data-reliability.pdf, page 5 (Vector Search) | identity-security.pdf, page 3 explicitly requires the same document scope for vector searches, graph traversal and PDF previews. | The accepted source contains the scope rule; the second labelled document is not retained. |
| `h10`: signed session versus document authorization | identity-security.pdf, page 3 (Document Access Control) | Pages 1 and 2 distinguish identity from permissions and require server-side checks on document operations; page 4 explains session limitations. | Potentially sufficient overlapping evidence, but the three-page label remains incomplete. |

A development-only diagnostic scored every overlapping two-sentence window on the missing `q06`, `q13` and `q16` pages using the cached local cross-encoder. Best logits were **-4.169, -5.201 and -5.920**, respectively: none passes the unchanged combined threshold with its recorded vector score. Merely adding more windows does not recover these pages and would add inference work. This experiment was not used to relax the threshold or alter labels.

These observations are an evidence audit, not human adjudication or proof of correct generated answers. For example, `q06` also admits an access-control passage containing prerequisite terminology; that is a reason to evaluate relevance/answer quality separately, not to declare the system perfect. Original expected-source labels remain unchanged. A later label review must be versioned and must not replace these strict metrics retroactively.

## Measurements

Development and diagnostic validation were run first, then the implementation was frozen for the final full run. All three corpus sizes (10, 50, 100 PDFs; 32, 112, 212 chunks) have the same counts:

| Measure | Pre-Phase-1 baseline | Initial Phase 1 | This follow-up |
| --- | ---: | ---: | ---: |
| All labelled source pages retained | 29/35 | 31/35 | 31/35 |
| Development source completeness | 12/17 | 14/17 | 14/17 |
| Diagnostic-validation source completeness | 9/10 | 9/10 | 9/10 |
| Previously authored held-out source completeness | 8/8 | 8/8 | 8/8 |
| Unsupported questions refused | 10/10 | 10/10 | 10/10 |
| Request errors | 0 | 0 | 0 |

No additional aggregate gain is claimed: the existing 45-question dataset does not exercise citation-budget starvation or a conflicting chosen excerpt. Those fixes are demonstrated by focused deterministic regressions, not by inflating the benchmark. Corpus byte hashes, dataset hashes and thresholds match the baseline. Final service-code hashes match the working tree. Every labelled passage fragment remains present.

Reports: [development/validation JSON](evidence-selection-followup-development.json), [final JSON](evidence-selection-followup.json), [final summary](evidence-selection-followup.md). The earlier baseline and Phase 1 reports remain intact for comparison.

## Verification and limits

- **330 backend tests pass**, up from 321: seven additional citation-selection/identifier-boundary regressions and two pipeline-diagnostic tests.
- **9 frontend tests pass**; TypeScript/Vite production build passes.
- Python compilation and `git diff --check` pass.
- Nine new regressions cover starvation, contiguous source IDs/original-input preservation, non-greedy bounded coverage, keeping an already-complete ranked selection, below-threshold evidence, ordinary non-identifier questions, conflicting selected excerpts, missing identifiers, and stage diagnostics (some assertions share a test).
- No LLM generation or hosted Cohere request was made. No secrets, production data, ingestion state, database schema, document permissions or deployment configuration changed.

Reproduce the final run with cached models:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python scripts/evaluate_retrieval_scaling.py --documents 10 50 100 --profiles identifier_10_verified --output evaluation/evidence-selection-followup
.venv/bin/python -m unittest discover -s tests -q
npm run test:frontend
npm run build
.venv/bin/python -m compileall -q app scripts tests
git diff --check
```

Limitations remain: synthetic authored PDFs, warm cached local models, in-memory Qdrant, sequential evaluation, and previously authored held-out questions. These results do not measure live hosted reranker calibration, generated-answer correctness, production throughput or independent customer-data accuracy. Nothing was committed, pushed or deployed.
