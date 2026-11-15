# Answer generation evaluation — approval preflight

Subsequently approved and completed: see [actual capture and findings](answer-generation-assessment-current.md). The JSON manifest below remains the immutable pre-execution request plan; its original zero-call status is historical.

Prepared 15 November 2026. **No provider requests have been made.**

The existing 12 authored quality cases contain 9 supported questions, 2 missing-attribute refusal challenges, and 1 no-source refusal. The first 11 can test model synthesis with known source excerpts; the last needs no model call. Expected claims are review checklists and are excluded from model request payloads. This is oracle-context synthesis diagnosis, not retrieval quality or unseen-question accuracy.

| Proposed limit | Value |
| --- | ---: |
| Hosted requests, total |11|
| Retries / failover |0 / disabled|
| Completion tokens per request |1,200|
| Completion tokens, maximum total |13,200|
| Prompt characters, exact text total |22,063|
| Input tokens, rough characters/4 estimate |5,521|
| Supplied literal source excerpts |21|
| Source / excerpt bounds |5 sources, 900 characters each|

The limits above are a proposed capture budget, not evidence that calls were made or that an execution tool has enforced them. Tokens depend on the provider tokenizer; this is not a dollar-price estimate. The selected local route is `groq`, model `openai/gpt-oss-20b`. No secret or private PDF is included.

## What the first capture would establish

- What the model actually returns on direct, comparison, multi-source and refusal cases.
- Whether source indices/pages are valid under the new mechanical checks.
- Actual response latency and reported token consumption for these bounded samples.
- A hash-bound worksheet for a real reviewer to judge claim support, missing details and refusal correctness.

Prompt/citation source hashes, corpus/PDF hashes, exact proposed request payloads, expected-claim checklists and local no-source action are in [the JSON manifest](answer-generation-preflight.json). Once generated responses exist, use the existing `scripts/evaluate_quality.py --responses ...` flow to audit them and create review worksheets; keep review scores unknown until actual review.

The known 12-case dataset is diagnostic, not heldout. A separate frozen quality dataset is needed before reporting unseen generalization gains. The no-source case should be recorded as a local refusal rather than presented as a model refusal. Production retrieval would already block some absent-equipment facts; these oracle cases deliberately test synthesis in isolation.

## Approval boundary

The earlier user instruction was “Offline checks only for now.” Preparing this pack stays within that instruction. Running its hosted requests consumes provider quota and requires explicit approval. No current Step4 code or production data was changed by this preparation. No capture, commit, push or deployment was performed.
