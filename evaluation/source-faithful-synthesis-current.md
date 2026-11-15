# Source-faithful synthesis correction

Implemented locally on 15 November 2026 after the approved diagnostic capture.
The 11 original model outputs and their reports are unchanged. No new hosted
requests were made, and the stronger prompt has not yet been tested on model
outputs. These are implementation/accounting checks, not answer-accuracy gains.

The generation prompt now explicitly limits examples to the source, preserves
qualifiers/negation/conditions, covers supported requested parts and material
limitations, and avoids repeating facts in multiple sections. Missing information
must be stated; the canonical refusal applies only when none of the requested
parts can be answered. The rules are generic and contain no diagnostic answers.

Provider admission now counts assembled system+user text, including source
headers, the bounded graph and Gemini's separator. It follows instance prompt
overrides on all existing provider paths. The offline quality tool accepts an
optional `--cases` file and rejects missing/wrongly typed fields, bad source pages
and duplicate expected-claim IDs before capture or scoring. Default cases and
existing human-review files are preserved.

| Verification | Actual result |
| --- | ---: |
| Baseline backend tests |749 passed|
| Final backend tests |765 passed|
| New focused test methods |16 passed|
| Existing provider routes exercised |5|
| Known context/provider text-count replay |33/33 match|
| Mocked Gemini wire-text accounting |Passed|
| Frontend tests / TypeScript+Vite build |9 passed / passed|
| Saved captures audited without regeneration |12|
| Valid citation labels / literal source passages |17 /21|
| New hosted requests / human semantic reviews |0 /0|
| Fresh reserved quality questions generated or scored |0/8|

The old app counter counted 8,583 characters for 22,063 characters of assembled
text across the 11 known Groq contexts. The new generic prompt is longer: system
text is 2,109 versus 1,052 characters, with 33,690 total assembled characters on
those contexts. All are now counted. This is an accounting correction and an
explicit prompt-size tradeoff, not measured model tokens or a cost reduction.

One fresh review found missing Gemini separators and malformed-case acceptance;
it also noted instance dispatch and partial-refusal ambiguity. One correction
pass fixed all four findings, with no second review. A temporary mock-client
base URL was corrected so the actual Gemini payload test could execute. A prior
approval-service usage failure prevented the first patch attempt; the user then
requested continuation and the normal approval path succeeded.

## Evaluation boundaries

- Prompt rules are instructions,not proof that models follow them. No regenerated candidate answer,semantic improvement,or hallucination rate was measured.
- The earlier 11-request approval is fully consumed. Candidate/fresh heldout model captures need a new manifest and explicit quota approval. The old preflight is bound to the old prompt and must not be reused.
- Eight fresh questions are assistant-authored review checklists,not independently human-labelled gold. They remain ungenerated/unscored.
- Stronger guidance increases system text from 1052 to 2109 characters; eleven known Groq contexts increase from 22063 to 33690 assembled characters. Live quality,latency and cost tradeoffs are unmeasured.
- Text-length accounting is not exact provider tokenization,billing,protocol framing or a new completion cap. The provider routes and existing reservation/output-allowance policy are unchanged.
- Known q13/as01 retrieval misses and broader semantic entailment remain separate. No evidence threshold/fact/source/auth/READY/worker/deletion/store lifecycle change.
- No additional hosted inference,new dependency,environment/secret change,production document mutation,commit,push or deployment.
- Individual focused unittest runs report an existing unused global Neo4j-driver ResourceWarning plus PyMuPDF SWIG deprecation warnings. Full suite exits successfully;no application resource lifecycle was changed.

## Next measurement

The new [eight-case quality set](quality-source-fidelity-heldout-v1.json) was
frozen before prompt edits and remains reserved. After a separate bounded quota
approval, capture candidate outputs with a new prompt-bound manifest, then audit:

```bash
.venv/bin/python scripts/evaluate_quality.py \
  --cases evaluation/quality-source-fidelity-heldout-v1.json \
  --responses /path/to/new-captures.json \
  --output /path/to/new-audit
```

Do not reuse the consumed 11-request budget or report the old captured answers as
candidate output. Human claim support, citation correctness and completeness
still require real hash-bound review. The next live comparison is pending, not
part of this offline pass.

[Exact before/after hashes, commands, accounting rows and limits](source-faithful-synthesis-current.json).
All changes remain local, uncommitted and undeployed.
