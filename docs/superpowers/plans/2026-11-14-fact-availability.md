# Fact availability implementation plan

Goal: prevent named-equipment lookup questions from admitting merely topical
passages when manufacturer, voltage or serial-number facts are missing. Retain
literal citations, .35 threshold, identifiers, auth/READY scope and all lifecycle
boundaries. This is a conservative explicit-fact checker, not semantic truth
verification or a universal answerability classifier.

Spec: ANSWER_QUALITY_CODE_AUDIT.md step3, user “so implement that”. The existing
q13/as01 source/paraphrase failures remain regressions; do not invent unsafe
threshold changes to force them to pass.

1. Freeze new authored development/heldout questions and supplemental equipment
   facts before implementation. New heldout is unrun until final rule freeze.
2. RED→GREEN pure fact requests/value/subject support and citation integration.
   A fact requires a concrete positive statement tied to the requested identifier
   in the literal cited text. Brand is not manufacturer; output is not input;
   unknown/negated/example facts and other devices do not satisfy a request.
   Generic definitions and unsupported question shapes retain the original path.
3. Reserve fact witnesses using the existing bounded coverage selector. At most
   eight combined fact/remaining-identifier requirements plus two facets preserve
   the existing1024-state bound. Fail closed on larger explicit fact requests.
4. Extend existing offline evaluator with authored supplemental PDF corpus. Keep
   original equipment fixture bytes unchanged; bind loaded baseline rerank's
   evidence check to the matching baseline module rather than current code.
5. Development and existing regression benchmarks, full suite/frontend/build,
   one fresh review, one RED→GREEN correction pass for important findings.
   Freeze rule before first new holdout measurement; do not tune afterwards.
6. Record exact metrics, coverage limits and remaining valid refusals. Keep local;
   no commit/push/deploy, environment changes, production data or hosted inference.

Files: new app/services/fact_support.py, tests/test_fact_support.py; integration
in citation_service.py and evaluator; authored JSON fixtures and measured reports.
No new dependencies or changes to retrieval scope, providers, graph extraction,
state machines, transactions, compensating deletion or resource management.

Review focus: cross-entity binding, input/output and brand distinctions, negation/
examples/unknown values, fact clipping between score and citation, multi-attribute
and comparison coverage under five citations, unchanged legacy behavior and fair
baseline isolation. Coverage is deliberately limited to explicit identifier slots;
open-ended factual entailment remains for future semantic evaluation.

Historical start: baseline and TDD were pending. New branch preserves the deployed d871ad1 work.

- Baseline701 tests pass(3.883s). RED contracts observed missing facts admitted,
  wrong-device/role/unknown/example/context leakage, clipped values, cross-facet
  borrowing and generic company descriptions. They passed after explicit binding
  and bounded coverage selection. Current focused suite22 tests passes.
- Original equipment fixture byte hashes for all6 devices unchanged after PDF
  helper extraction. The evaluator's baseline rerank gate is bound to its loaded
  baseline citation module. Isolation test uses a disposable local Git repo, so
  CI does not require historical commits in a shallow checkout.
- Initial model development:5/5 source completeness retained; missing/wrong facts
  refusals0/4→4/4. Original benchmark remains34/35 and10/10 refusals. Fresh heldout
  is still unrun. Both numeric scores remain uncalibrated evidence heuristics.
- Ruling: recognize explicit named-identifier manufacturer/voltage/serial slots
  only; generic definitions, open questions and ambiguous compound forms fall
  back. This avoids pretending regex is universal entailment; some valid free-form
  or lowercase/ambiguous names may conservatively refuse. q13/as01 remain outside
  the safe correction in this pass. No global threshold reduction.

- One fresh read-only review found3 important defects: suffix uncertainty,
  foreign/accessory/future subject binding and placeholder values.13 failing
  subcases reproduced them; one correction pass makes all25 focused methods pass.
  Positive independent sentences/semicolon fields and a device that itself is an
  adapter remain supported. No second review.
- Final minor(deferred): final comparison selection may cite a globally valid
  fact witness from another facet instead of retaining the same facet's witness.
  Literal requested facts are still verified and the per-facet guard has passed;
  no false admission was demonstrated for this attribution detail.
- Rule and dataset hashes frozen in /private/tmp/cg-fact-rule-freeze.json before
  any new heldout evaluation. Generic/free-form, lowercase active manufacturer
  names, complex polarity/scope descriptions may conservatively refuse; this
  explicit-slot policy is not a semantic entailment model.

- Final scoped acceptance complete locally:726 backend methods pass;9 frontend
  tests and TS/Vite build pass.82 candidate cases each pass four exact invariants.
  New frozen fact holdout5/5→5/5 sources,0/5→5/5 refusals; development5/5 and0/4→4/4.
  Original34/35 and10/10 unchanged; consumed context5/6 and2/4→4/4; consumed old
  answer-selection4/5 and2/3→3/3. No rule/data hash changed after first fact holdout.
- Ruling: report all10 existing context cases, including its historically consumed
  held_out rows, rather than just the5 development cases. The additional old
  idempotency-versus-HTTPS comparison miss remains. All baseline/current supported
  per-case source metrics match exactly. New fact holdout is now consumed.
- One final regression invocation incorrectly passed the scaling manifest as a
  challenge question file; schema validation rejected it before measurement.
  Corrected to the original/default CLI with no --questions and reran successfully.
  Some regression jobs overlapped; do not compare latency or claim throughput.
- Frozen source/data hashes, per-case outcomes, exact command templates and scoped
  limits are saved in evaluation/fact-availability-current.json and .md. No commit,
  push, deployment, hosted inference or production/environment mutation. Next is
  answer completeness/citation discipline; broader fact entailment stays pending.
