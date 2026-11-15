# Source-faithful synthesis implementation plan

> Execute inline with superpowers:executing-plans; one final fresh read-only review.

Goal: address the diagnostic answer failures with source-faithful concise guidance,
correct prompt-size admission estimates,and a usable fresh quality holdout path.
Spec: ../specs/2026-11-15-source-faithful-synthesis-design.md
Architecture: existing SynthesisService prompts and provider hooks; optional
--cases in existing offline quality CLI. Python/unittest,no new dependency.

Ruling: user "next" authorizes this already agreed offline correction; preserve
in-place uncommitted Step4 work rather than making a new worktree or release.
The11 captured outputs and earlier raw reports remain historical immutable data.

Constraints: zero hosted calls,new judge/repair calls,production data/env changes,
commit,push or deploy. No API/schema/auth/READY/store/lifecycle changes. No semantic
score or fresh heldout result without actual generation and real review.

Review focus: all provider call sites must meter their actual assembled text;
bounded graph text must stay bounded; instruction updates must stay generic and
preserve partial answers/refusal; optional dataset must not silently fall back to
the old cases or overwrite reviews; heldout must not be generated or scored.

- [x] Save exact pre-change source/capture hashes; freeze8 fresh quality questions.
- [x] RED→GREEN prompt-text count,graph bound,all five provider routes and generic
  source-fidelity/completeness/qualifier/conciseness contracts. Change only prompt
  guidance and the existing character counter,no heuristic semantic veto.
- [x] RED→GREEN --cases custom/relative dataset selection,source validation and
  dataset hash/default compatibility,using disposable non-heldout fixtures only.
- [x] Run full backend/frontend/build and audit saved live responses offline.
  One fresh read-only review; reproduce important findings and one correction
  pass,never a second review. Freeze final hashes and record honest limitations.

Historical start: baseline verification was pending; current branch codex/answer-citation-contract
above8de7beb; earlier749-test result is historical until rerun this increment.

Final ledger:
- Baseline749 tests passed(4.089s). First11 methods reproduced17 behavioral RED
  subcases in prompt guidance,full text/provider counts and ignored custom data.
  Initial integration760 passed after10 mocked source fixtures gained the same
  document_name metadata required by the real prompt; assertions were preserved.
- One fresh read-only review:2 Important (Gemini separator,malformed/duplicate
  case claims),2 Minor (instance-only overrides,ambiguous partial refusal). RED
  regressions expanded to16 methods; one correction pass fixed all4 findings.
  A Gemini mock's missing base URL was corrected before payload validation ran.
  Final16 methods and765 total pass(2.941s); no second review.
- Both Gemini paths include the actual two-character separator; other adapters
  count their actual separate message text. Counter now follows instance dispatch.
  Offline33/33 provider-context text comparisons match. Old Groq app count8583
  versus22063 assembled; new prompt33690 assembled,all counted. This is not billing.
- Saved live outputs audited offline:12 contracts,17 valid labels,21 literal
  source passages; no new generation or human review. The eight frozen questions
  were neither generated nor scored. Old captured outputs/reports and protected
  evidence/fact/rerank source hashes are unchanged.
- Nine frontend tests and TS/Vite build passed(Vite4.85s) during this increment;
  final review changes only Python. Syntax,diff and final hash checks passed.
- A usage-limit error prevented the first auto-reviewed patch action; no mutation
  happened. User requested continue and the same normally reviewed action then
  succeeded. No approval bypass,new hosted calls,production changes or release.
- Scope complete locally: prompt/accounting/evaluator wiring. Actual candidate
  answer quality remains unmeasured. Details: evaluation/source-faithful-synthesis-current.json/.md.
  Next live comparison needs a new manifest/budget; current11-call budget is used.
