# Explicit fact availability — local verification

Measured 14 November 2026 against deployed source baseline `d871ad1`. This change is local and uncommitted.

A relevant passage no longer suffices for recognized equipment manufacturer, voltage or serial lookups. The final literal citations must contain an explicit positive fact for the correct equipment and property. Input/output and brand/manufacturer are distinct. Missing, uncertain, example, wrong-subject and placeholder values do not unlock synthesis.

| Dataset | Required sources before → after | Unsupported refusals before → after |
| --- | --- | --- |
| Fact development | 5/5 → 5/5 | 0/4 → 4/4 |
| Fresh fact heldout v1 | 5/5 → 5/5 | 0/5 → 5/5 |
| Original benchmark | 34/35 → 34/35 | 10/10 → 10/10 |
| Existing context regressions | 5/6 → 5/6 | 2/4 → 4/4 |
| Previously consumed answer-selection regressions | 4/5 → 4/5 | 2/3 → 3/3 |

The new fact heldout was authored/frozen before implementation; final reviewed rule hashes were frozen before its first run and remained unchanged afterwards. It is now consumed. Other historical heldout labels above are regressions, not fresh evidence. All five supported development and five supported new heldout cases also retain their exact requested fragments. Both runs used identical PDF hashes; the original six generated equipment PDFs remain byte-identical.

Verification: 726 backend tests pass (3.975s), 25 new backend methods; nine frontend tests and TypeScript/Vite build pass. 82/82 exact checks each pass for traced/untraced output, primary citation prefix, primary refusal and primary evidence score/level. These preserve the new primary gate against optional support, not the old baseline's intentionally tightened refusals.

The existing context development split specifically retains 3/3 supported sources and improves refusals 1/2→2/2. Its entire consumed context set is shown above. q13, as01 and the old idempotency-versus-HTTPS comparison source miss remain unresolved.

## Implementation and review

- `fact_support.py`: bounded explicit request recognition and literal fact/subject verification; no external model call.
- `citation_service.py`: reserve fact witnesses inside the existing five-citation coverage selection, verify final clipped text and fail closed when an explicit fact is absent. At most eight independent fact/remaining-ID constraints plus two facets keep the existing 1,024-state bound.
- Source-safe trace reasons distinguish missing facts from earlier search, ranking and score losses; raw equipment values are not added to production traces.
- Supplemental synthetic PDF fixtures and isolated baseline-module loading support reproducible comparisons. Baseline rerank calls its matching evidence module, not the current guard.
- One fresh review found trailing uncertainty, foreign/future/accessory subject binding, and placeholder gaps. Thirteen failing subcases reproduced them before one correction pass; all focused tests pass.

## Limits and next work

- Explicit digit-bearing equipment identifier lookups for manufacturer, voltage and serial only; ambiguous or generic questions keep existing policy. Not a universal semantic fact checker.
- Conservative handling may reject valid free prose, lowercase active manufacturer names, unusual tables or complex polarity.
- One authored sequential local-model run per case using in-memory Qdrant brute-force search; no hosted ANN, provider synthesis, real-user concurrency or human semantic review. Some final regression jobs overlapped; latency is not compared.
- Source completeness and exact expected fragments are mechanical evidence checks, not generated-answer correctness.
- Global evidence threshold remains 0.35 and citation limit five. Auth, READY scope, providers, stores, lifecycle and deletion are unchanged.
- Minor deferred: final comparison selection may choose a globally valid requested-fact witness from another facet; each facet separately passed and final literal facts are verified, but facet-to-witness attribution could be stricter.
- Failed optional rerank attempts remain omitted from legacy offline work counters. No work/billing comparison is claimed.
- q13 and as01 remain supported misses; existing context comparison about idempotent ingestion and HTTPS also remains missing a required source. No unsafe threshold adjustment.
- No hosted inference, production mutations, environment/secret edits, commit, push, deployment or feature activation.

Next: improve answer completeness, validate generated citation indices and fallback coverage, then collect separately budgeted generation captures with actual human claim-support review. Broader fact entailment and supported paraphrase misses need their own development data and a fresh frozen holdout.

## Reproduce

Use the cached local models `sentence-transformers/all-MiniLM-L6-v2` and `cross-encoder/ms-marco-MiniLM-L-6-v2`, CPU with two Torch threads, `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`. The exact command templates, dataset mappings, source/data/artifact hashes, per-case outcomes and mechanical checks are in [the JSON report](fact-availability-current.json). Original/default corpus uses no `--questions` argument. No backend static type checker is configured; TypeScript checking covers the frontend.
