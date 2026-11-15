# Answer completeness and citation discipline — local verification

Measured 15 November 2026 against released source `8de7beb`. This increment is
local, uncommitted and undeployed. It implements the mechanical part of Step 4;
actual generated-answer correctness and completeness are still unmeasured.

A provider answer must have usable text and at least one valid source label.
Every cited index must exist, and any explicit page must match that source's
known positive integer page. Missing/unfinished/malformed labels, wrong pages,
citation-only answers and question echoes with citations use the existing
InvalidAnswerError/failover path. The canonical refusal remains allowed without
citations. The offline audit shares this parser and records `answer_contract_valid`
without assigning a semantic correctness score.

| Authored format cases | Valid outputs retained before → after | Invalid outputs rejected before → after |
| --- | --- | --- |
| Development |4/4→4/4|1/12→12/12|
| Fresh frozen heldout |3/3→3/3|0/7→7/7|

These are authored output contracts, not real model generations or independently
human-reviewed semantic answers. Data froze before implementation; reviewed
source froze before the first heldout run and stayed unchanged afterward.
The heldout is now consumed; future tuning needs a new heldout.

Fallback now retains 5/5 authored accepted excerpts instead of 2/5, preserving
source order and original citation indices. It also retains a fact at the end of
an 857-character accepted passage that the old 600-character limit dropped.
It labels excerpts as such, says they may not cover all requested details, escapes
source Markdown/entities and discloses any shortening outside the normal bound.
The prompt now requests a direct answer, cited details and explicit missing parts;
this does not demonstrate actual model adherence.

Verification: 749 backend tests pass vs 726 baseline, including 23 new methods;
nine frontend tests and TypeScript/Vite build pass. The existing deterministic
quality capture has 12 responses, 21 valid source/page references,21/21 literal
source passages and 12/12 mechanical contracts; provider calls 0, reviewed answers 0.
Human correctness, citation support and completeness remain null.

One fresh read-only review found two Important rendering defects. Three failing
subcases reproduced invisible Markdown reference definitions and unescaped GFM
strikethrough/entity expansion. One correction pass fixed them, with seven checks
using the actual installed ReactMarkdown/GFM/ReactDOM server renderer. No second
review. Six existing provider mocks were updated to supply valid source evidence;
their failover/circuit/admission assertions remain in place.

## Limits and next work

- Mechanical format and reference checks only. A valid citation can still fail to support a claim. No live generated answers or human semantic review were performed.
- Direct-answer structure, per-claim citations and missing-detail disclosure are prompt instructions; actual model adherence and semantic completeness remain unmeasured.
- Exact canonical refusals are allowed without citations. Correctness of those refusals is unmeasured; q13/as01 and wider fact-entailment work remain separate.
- This is not a full Markdown/HTML semantic parser. The two reviewed rendering defects are covered; arbitrary hidden or code-context text still needs broader renderer-aware evaluation.
- Fallback displays accepted excerpts, not a generated answer. It is capped at five sources and 900 normalized characters per source. Oversized direct inputs are explicitly labelled shortened; normal accepted excerpts already use the 900-character bound.
- Invalid outputs can invoke an existing budgeted backup provider. No repair, judge or extra retry stage was added; routing, usage observation, circuit and cancellation behavior are retained.
- The public API, auth, READY scope, evidence/fact thresholds, source order, graph/store/worker/deletion lifecycle and production flags are unchanged. The retrieval benchmark was not rerun; unchanged source bytes are recorded, not claimed as fresh model metrics.
- New authored heldout cases were frozen before implementation and first measured after reviewed-rule freeze. They are now consumed; no source/data tuning followed measurement.
- One fresh review, two Important findings, three failing subcases reproduced, one correction pass, no second review. Six preexisting successful-provider mocks were supplied actual cited source text rather than weakening validation.
- Local and uncommitted. No push, deploy, hosted inference, production test data, environment/secret changes or new dependencies.

Next: obtain separately budgeted real generation captures and actual human reviews
of claim support, missing details and refusal correctness. The existing hash-bound
review workflow remains available. Do not label generated scores as human review
or this structural pass as a measured hallucination/answer-accuracy gain.

Exact commands, per-case before/after outcomes, data/rule hashes, test results,
renderer inputs/HTML and unchanged evidence-source hashes are in
[the JSON report](answer-contract-current.json).
