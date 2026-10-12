# Phase 1 — scored context and harder evidence checks

## Verified fixes

Two regressions failed before the changes and pass afterward:

- **Scored text differed from generated evidence.** The primary rerank window could contain 1,600 characters, but citation construction selected another 900-character excerpt afterward. A high score could therefore reflect facts absent from the generation prompt. Rerank windows now share the existing 900-character evidence limit; the selected literal text reaches citations unchanged. This does not raise the generation token budget or lower the evidence threshold.
- **A final sentence could lose preceding context.** The window builder admitted the last sentence on its own, even when the preceding sentence warned not to perform the described operation. Supplemental windows now contain two adjacent sentences. This prevents that particular orphan-window case, not every possible context/negation error.

Both local and mocked hosted reranking paths are tested. The original source text, metadata, five-citation limit, identifier verification, scope and provider request count remain unchanged. Model tokenization may still impose its own limits; matching request text is not proof that every token was attended to or that every resulting claim is correct.

## Measurements, without changing the original labels

| Dataset | Development source coverage / gate refusals | Held-out source coverage / gate refusals | Overall source coverage / gate refusals |
| --- | --- | --- | --- |
| Existing 45-question benchmark | 14/17; 5/5 | 8/8; 2/2 | 31/35; 10/10 |
| New 10-question context challenge | 2/3; 1/2 | 2/3; 1/2 | 4/6; 2/4 |

The existing benchmark also includes diagnostic validation: **9/10 source coverage; 3/3 gate refusals**. Results are identical at 10, 50 and 100 PDFs (32, 112 and 212 chunks). Its source coverage and refusals did not regress, but no additional aggregate improvement is claimed. The new challenge's development results were also **2/3 and 1/2 before** the context-window changes.

The new challenge's labels were checked against the existing authored computing and equipment PDFs and fixed before the window-policy change. Development cases were inspected first; held-out results were inspected only after freezing the implementation. This set is separate from the original benchmark, not a replacement. Both sets are authored fixtures, not independently annotated customer data.

All emitted citation passages exactly match text submitted to the reranker: **76/76, 71/71 and 71/71** on the original benchmark, and **18/18** at every corpus size on the challenge. This measures a text-contract invariant, not citation correctness or answer accuracy. Controlled long-passage tests exercise the clipping mismatch; the small benchmark PDFs alone do not establish long-document accuracy.

Reports: [original benchmark final JSON](evidence-selection-context-final.json), [challenge final JSON](evidence-context-challenge-final.json), [challenge development before](evidence-context-before-development.json), [challenge development after](evidence-context-after-development.json). Corpus hashes match across final runs and the preceding Phase 1 run; original dataset hashes, threshold 0.35 and final service-code hashes were verified. No request errors occurred.

## Remaining failures exposed by harder questions

- `ec01`, development: a comparison of TLS certificate identity checks and PDF authorization loses the TLS page at the evidence gate. Unlike several earlier redundant-page labels, this loses one side of the comparison.
- `ec06`, held-out: a comparison of ingestion idempotency and HTTPS retains idempotency evidence but loses the HTTPS page at the gate. This case is now a held-out observation, not a fresh untouched question for subsequent tuning.
- `ec04`, development: “Who manufactured PMP-104?” admits equipment passages that do not state a manufacturer.
- `ec09`, held-out: a request for PMP-105's factory serial number admits equipment passages that do not state a serial number.

The last two are **retrieval-gate false positives**, not observed hallucinated LLM answers: this runner never calls generation. The LLM might still refuse them, but the retrieval gate does not prevent the generation call. Topic relevance and exact identifier presence do not prove answerability. The earlier 10/10 gate-refusal result must not be generalized to these harder missing-attribute questions.

No manufacturer-specific keyword rule, forced weak evidence, threshold relaxation or question-specific patch was added. The remaining work is to evaluate comparison-facet coverage and answerability-aware filtering on more development examples, with new held-out questions for any later tuning. Adding more overlapping sentence windows alone does not solve this problem. Phase 1 is **not fully closed on broader evidence quality**.

## Configuration check

The default local reranker name and the benchmark's canonical name differ, but both cached repositories resolve to the same model/configuration blobs. No configuration change was justified by this check. It does not verify remote alias behavior on an uncached installation. No model downloads or provider requests were made.

## Verification

- **334 backend tests pass** (four more than the preceding 330): exact scored/generated text, final-sentence context, hosted bounded-text mapping and authored label validation.
- **9 frontend tests pass**; TypeScript/Vite production build passes.
- Python compilation, `git diff --check`, dataset/corpus/code-hash checks pass.
- Ingestion, retries, deletion, READY scoping, authentication, rate limits, storage and deployment configuration were not changed.
- Changes remain local; nothing was committed, pushed or deployed.

Reproduce with cached models, without provider quota:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python scripts/evaluate_retrieval_scaling.py --documents 10 50 100 --profiles identifier_10_verified --output evaluation/evidence-selection-context-final
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python scripts/evaluate_retrieval_scaling.py --documents 10 50 100 --profiles identifier_10_verified --questions evaluation/questions-evidence-context.json --output evaluation/evidence-context-challenge-final
.venv/bin/python -m unittest discover -s tests -q
npm run test:frontend
npm run build
.venv/bin/python -m compileall -q app scripts tests
git diff --check
```

Limitations: in-memory Qdrant, cached local MiniLM models, authored PDFs, sequential single-run timings, no live hosted reranker calibration, no generated answers or production load test. The final two evaluation processes ran concurrently, so their latency numbers should not be used for before/after performance claims.

## Further development-only experiments

Exhaustive two-sentence and neighboring three-sentence checks on the missing TLS page in development case `ec01` did not improve on its whole-passage logit of **-1.036**. Its combined evidence score remains **0.3321**, below 0.35. The strongest tested three-sentence window has logit **-1.369**. Reordering windows by rare terms cannot rescue this case when none of those windows clears the existing gate, so no unverified rarity/window policy was introduced.

Meanwhile, absent-manufacturer passages in `ec04` score **0.7548/0.6817** and absent-serial-number passages in previously observed `ec09` score **0.8608/0.8625**. These observations show why a global threshold change is not a reliable remedy: irrelevant-to-the-requested-fact passages can score substantially above useful complementary evidence. They still do not demonstrate a generated hallucination, because generation was not called.

A separate comparison-facet prototype splits only explicit **Compare/Contrast A with B** development questions, reranks the same fixed candidate pool for each part, requires evidence for both parts, and reserves a citation for each before filling the five-source budget. The existing threshold and literal citation policy are retained.

- Development source completeness: **2/3 → 3/3**; all three labelled fragment checks pass.
- Development evidence-gate refusals: **1/2 → 1/2**. Missing-attribute checks are not solved.
- No search, generation, provider request or production data access was performed; this is an isolated reranking/selection experiment, not an end-to-end or independent held-out improvement.
- The live API has **not** adopted this prototype. The Render Blueprint configures Cohere; supporting two distinct facet queries there would require two rerank requests instead of one for matching comparison questions. Local scoring can reuse the cached model, but still adds inference work.
- **336 backend tests now pass**, including two prototype boundary tests. Previously verified frontend tests and build remain unchanged; Python compilation and diff checks pass.

Reproduction and results:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python scripts/experiment_evidence_facets.py
```

See [the prototype](../scripts/experiment_evidence_facets.py) and [its measured development report](evidence-facet-development-experiment.json). It verifies source hashes and labels against the fixed candidate report and rejects ambiguous multi-chunk-per-page reconstruction. New untouched questions and a hosted cost/latency check are required before promoting the comparison experiment to the API. The production behavior and original benchmark scores have not changed.
