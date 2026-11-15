# Answer contract implementation plan

> Execute inline using superpowers:executing-plans; one final fresh read-only review.

Goal: add mechanical citation validation and preserve all bounded fallback excerpts.
Architecture: shared pure parser in app/services/answer_contract.py, used by
synthesis_service and scripts/quality_metrics. Existing failover handles errors.
Spec: ../specs/2026-11-15-answer-contract-design.md
Stack: existing Python, unittest, mocked providers, existing frontend checks.

Constraints: offline/mocks only; no new provider requests/repairs, no threshold,
API/auth/READY/store/lifecycle changes. Five-source,900-character excerpt bounds.
Canonical refusal remains allowed. A correct reference is not semantic support.
Current clean baseline8de7beb; continue on local codex/answer-citation-contract.
Ruling: apply the already authorized Step4 in place on a new branch; no new
worktree, design approval loop, commits or deployment in this increment.

Review focus: malformed/unclosed labels, wrong/unknown page provenance, echoes
with citations, reference labels inside source quotes, and validation on every
provider route including fallback. Preserve failover classification/cancellation.

- [x] Freeze new authored development/heldout fixtures before product changes.
- [x] RED→GREEN runtime invalid/missing/page/echo/reference cases and every
  provider route, add shared citation_references(answer,sources) parser and
  validate_answer_contract(answer,question,sources) raising ValueError. Runtime
  wraps contract failures as existing InvalidAnswerError; private two-arg helper
  compatibility retains the legacy empty/echo checks, but every route passes sources.
- [x] RED→GREEN fallback source3–5 retention, blank-index holes,900-character
  tails, shortened disclosures and escaped labels inside literal source excerpts.
- [x] RED→GREEN offline-audit parser agreement and prompt contract. Correct old
  mocked successful-provider fixtures that cite absent sources rather than
  weakening runtime source validation. No externally visible schema changes.
- [x] Baseline/full suite,frontend/build; one fresh review and one important
  correction pass with RED→GREEN; freeze rule, then first heldout run and report.
  No second review or tuning after heldout. Update current roadmap/ledger.

Ledger: baseline726 tests pass; implementation pending. Preserve existing fact
holdout as consumed; no new semantic accuracy or broad completeness claim.

Implementation ledger:
- Baseline726 backend tests pass(4.209s). Initial RED:8 behavioral failures and29
  missing-interface/module errors across21 methods; actual old outputs demonstrate
  invalid references admitted, fallback source3–5/tail facts omitted and no prompt
  completeness contract. A fixture used plain str for a SecretStr; corrected that
  synthetic fixture, then all21 new methods pass.
- Full integration initially had6 failures: older successful-provider mocks cited
  absent sources or supplied no citations. Updated only those fixtures with real
  synthetic passages/readable labels, preserving backup call counts, failure type,
  circuit and admission assertions. Full747 tests pass(4.856s).
- Development16 authored cases: valid4/4→4/4, invalid rejection1/12→12/12.
  Bounded fallback retains5/5 excerpts vs2/5 and retains an857-character passage's
  tail fact. New10 heldout cases remain unrun pending reviewed-rule freeze.
- Offline quality fallback capture:12 responses,21 references with no invalid
  indices/pages and21/21 literal source passages; all12 pass mechanical contract.
  Provider calls0; reviewed answers/claims0; correctness/completeness remain null.
-9 frontend tests and TS/Vite build pass(Vite9.66s). Evidence selection, fact guard,
  rerank, excerpt selection and query endpoint files are byte-identical to8de7beb.
- Ruling: newly invalid answers may trigger the existing quota-budgeted backup
  path; no new judge/repair/retry stage is added. No hosted calls were made here.
  This boundary must be explicit; mechanical labels are not claim entailment.
- One fresh read-only review requested; no heldout data opened by reviewer. Next:
  reproduce important findings, single correction pass, freeze rule and measure.

Final ledger:
- One fresh read-only review found two Important defects: invisible Markdown
  reference definitions and GFM strikethrough/entity activation inside fallback
  excerpts. Two new methods reproduced three failing subcases. One correction
  pass rejects definitions and escapes tilde/entity introducers. No second review.
- All 23 new methods and all 749 backend tests pass (4.213s). Actual installed
  ReactMarkdown/GFM/ReactDOM renderer verifies seven assertions for both defects.
  Nine frontend tests and TS/Vite build pass; frontend source did not change.
- Reviewed source/data frozen before first new heldout run. Heldout valid 3/3
  retained, invalid rejection 0/7→7/7; development valid 4/4 retained and 1/12→12/12.
  No rule/data tuning followed. The heldout is now consumed. Scope is structural
  format validation, not human semantic accuracy or model-prompt adherence.
- Final deterministic fallback quality audit: 12/12 mechanical contracts, 21 valid
  references and 21 literal source passages, zero provider calls/human reviews.
  Full reports/commands/hashes/limits: evaluation/answer-contract-current.json/.md.
- Evidence/fact/rerank/excerpt/query source files are unchanged from released8de7beb.
  All changes remain local and uncommitted, no deployment/production mutations.
  Next is approved-budget generation capture and actual human claim-support review.
