# Answer-quality step 1: explain evidence loss

Scope: user asked to begin step 1 of ANSWER_QUALITY_CODE_AUDIT.md, then continue.
Preserve source selection, scoring, thresholds, refusal and HTTP contracts. Offline
cached models/authored PDFs only. No production data, hosted calls or deployment.

1. Capture a clean baseline and add failing diagnostic contracts.
2. Add opt-in gate/citation decision hooks plus offline search/rerank adapters.
   Link normalized literal windows by hash and source position; no raw request,
   source text, filenames or document IDs in trace events. Do not log traces.
3. Reproduce the four known misses with cached local models; add frozen-score
   regression replays. Compare traced and untraced source/confidence outputs.
   Include comparison and missing-attribute cases; preserve a new unrun holdout.
4. Run all backend/frontend/build checks and one fresh read-only final review.
   Record measured findings and limits; leave verified changes local for review.

## Ledger

- Baseline: 677 backend tests pass, 3.951s. Original evaluator at 10 PDFs / 32
  chunks: 31/35 supported source-completeness, 10/10 unsupported refusals.
- Initial ten diagnostic contracts: seven first failed on missing trace module; six
  passed after implementation, and Qdrant fixture initially failed on unsupported
  context-manager use. Corrected test cleanup. Two subsequent RED contracts caught
  absent comparison cap/source detail tracing, then passed. Four frozen regression
  replays cover actual window selection and gate decisions with captured scores.
- Added optional trace only; production rerank/search code remains unchanged.
  Adapter returns the real search response/reranking output without another
  search or prediction. Same-input untraced evidence comparison is offline only.
- New holdout: questions-answer-selection-holdout-v1.json is fixed but UNRUN.
  q06/q13/q16/h10 are development regressions, including historical validation h10.
- Ruling: local branch codex/evidence-selection-tracing isolates this clean checkout
  without moving/copying archived files or creating duplicate running services.
- One fresh read-only final reviewer found two diagnostic gaps: raw search hits
  dropped during fusion were labelled search misses, and reconstructed acceptance
  could invent later stages after an early comparison/identifier refusal. Three
  new RED contracts reproduced both gaps. The single correction pass links raw
  hashed source identity and derives traced acceptance from actual gate events;
  coverage/final guards now explain skipped paths. All 13 contracts pass. No
  second review; no deferred important findings.
- Final backend: **690 tests pass** (2.745s); frontend **9 pass**; TypeScript/Vite
  build passes (Vite 4.55s). Diff/AST/report-hash checks pass. Both final model
  evaluations used `/private/tmp`, offline flags and cached models. Original
  outcomes remain **31/35 completeness, 10/10 refusals**; existing development
  challenge remains **3/3 completeness, 1/2 refusals**. All 50 exact source and
  confidence outputs equal untraced selection on the same ranked inputs; all 45
  baseline outcome fields match excluding timings/added diagnostics. New holdout
  remains unrun. Runtime/dependency limitations are in the report.
- Report generation initially used system Python without torch package metadata;
  reran successfully with project `.venv/bin/python`. This did not affect model
  evaluation or application state.
- Step 1 complete locally; no commit/push/deployment, hosted quota or production
  mutation. Step 2 evidence-context/refusal correction remains next.
  [Results](../../../evaluation/evidence-selection-trace-current.md).
