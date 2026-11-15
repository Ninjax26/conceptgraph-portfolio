# Current-prompt answer comparison preparation

Scope: implement a reproducible offline pack for the next agreed quality
measurement and commit it. Hosted execution needs a new explicit quota approval;
the previous 11-request allowance is consumed. Runtime prompt, retrieval, auth,
stores and ingestion remain untouched. No deployment requested this increment.

Acceptance: exact known source contexts and current request text, separate fresh
candidate cases, no baseline answer or checklist in model messages, literal
bounded sources, source/request hashes, explicit proposed budget, no inference
or fabricated metrics, exclusive output creation and regression tests.

- [x] Inspect clean released baseline d5626c2 and next-quality roadmap.
- [x] Write five failing tests; import fails before tool exists.
- [x] Implement builder and observe five passing tests.
- [x] Prepare manifest: 12 known cases (11 hosted), 8 fresh (8 hosted),
  56,393 prompt characters, maximum 22,800 completion tokens. No requests made.
- [x] One fresh read-only whole-increment review, then one correction pass if needed.
- [x] Full backend tests, syntax/diff checks and source/artifact integrity.
- [x] Commit scoped code, tests and sanitized preparation artifacts.

Ruling: preparation constructs fresh request text but generates/scores no fresh
answers and never sends review checklists. It does not consume the heldout as a
measurement; later prompt tuning must not use these questions as feedback. Cost
if wrong: future generalization claims would need another fresh set.

Known baseline is a historical stochastic sample. A later paired comparison can
identify output differences, but cannot attribute all differences to the prompt
or eliminate provider/model variation. Fresh candidate-only cases have no paired
baseline. Human review remains required for semantic claims.

Final review: one P2 question-identity gap fixed against the original approved
manifest; synthetic changed-question case reproduced RED then passed GREEN.
Five focused methods and all 770 backend tests pass. No second review. AST/diff
and source/request hash integrity pass. No runtime/frontend edits, new provider
calls, human reviews, production mutations, push or deployment. Frontend tests
were not rerun for this tooling-only change. Final source-identity checks also
confirm all preexisting evaluation artifacts and runtime files match d5626c2.
