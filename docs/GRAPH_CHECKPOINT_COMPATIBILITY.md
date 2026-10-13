# Versioned, source-bound graph checkpoints

Phase 3 increment, 13 October 2026. This is **per-upload checkpoint compatibility**, not a cross-upload cache or automatic continuation scheduler.

## Confirmed gaps fixed

- Old section keys hashed section identity, chunk IDs and text, but not the configured models, prompt or provider schema. A requested rebuild could silently reuse an extraction made under a different configuration.
- Recovery keys hashed source excerpts but omitted preceding concept names used in the recovery prompt. Changing those concepts could reuse an unrelated recovery result.
- Loading saved output directly through the normalizing Pydantic model could discard unsupported relationship types before the raw graph validator checked them. Checkpoints must not bypass the provider-output boundary.
- Reused checkpoint nodes previously carried saved provenance without rebinding it to the current source chunks.

## Current behavior

Section keys use `section:v2:<sha256>`; recovery keys use `global:v5:<sha256>`. Both fit the existing 80-character database column; no migration is required.

The fingerprint includes:

- Exact ordered source excerpts, chunk IDs, filename, page, heading, upload identity and section identity.
- Configured eligible provider route and model names. Groq still prefers Gemini as backup when configured; Cerebras remains the compatibility backup otherwise. A temporarily open vendor circuit does not change this identity.
- Actual extraction system prompt and provider schema, including the Cerebras schema variant when used.
- Context limits, output-token allowance and repair-enabled flag.
- An explicit checkpoint contract version for validator/provenance changes not represented by prompt/schema hashes. Maintainers must bump it when those rules change materially.
- For recovery, the actual recovery context, including preceding concept names.

API key values, request budgets, cooldowns and attempt execution tokens do **not** invalidate compatible results. Rotating a secret alone should not trigger repeat graph calls. Adding/removing an eligible provider or changing an active model does invalidate them. API and worker configurations must agree.

Before reuse, raw relationship types, endpoints and batch-size limits are checked before normalization. Every concept must cite a source chunk in this batch. Provenance is then rebound from current chunk metadata, while filtered-output diagnostics are retained. An explicit validated empty result remains a completed batch; an implicit empty or quota-limited aggregate does not.

Unknown, mismatched and malformed checkpoints are cache misses, not fatal pipeline errors. Accepted fresh results still pass the same provider validation and lease-fenced checkpoint save. Document state transitions, vector indexing, task-token fencing, deletion and cross-store compensation are unchanged.

## Compatibility and quota implications

- Existing checkpoint rows are **not deleted or migrated**. Existing READY graphs remain unchanged and queryable until a normal authorized retry is requested.
- Old unversioned rows cannot prove which model/prompt produced them, so the new implementation does not trust them on that retry. The first requested rebuild can require additional fresh graph calls within the existing batch/admission limits.
- Subsequent retries under the same contract reuse completed batches, including explicit empty responses. A quota stop preserves those new checkpoints; only missing compatible batches are requested next time.
- No automatic production retries, uploads, deletions or model calls were performed for this release.
- These keys identify an eligible model route, not a recorded actual winning provider/model per batch. Cross-upload caching still requires explicit outcome metadata, retention/ownership decisions and safe provenance rebinding; it is not implemented here.

## Verification

Reproduce the focused checks without external services:

```bash
.venv/bin/python scripts/evaluate_graph_checkpoint_compatibility.py
.venv/bin/python -m unittest discover -s tests -q
npm run test:frontend
npm run build
.venv/bin/python -m compileall -q app scripts tests
git diff --check
```

[Offline report](../evaluation/graph-checkpoint-compatibility-current.md): 20 passing contract checks, including a fresh Python subprocess consuming serialized output. No hosted LLM, embedding, reranking, storage or database calls. Full backend suite: 409 passing tests; frontend: nine passing tests; TypeScript/Vite build and compile/diff checks pass.

These are structural and compatibility checks on authored graph fixtures, **not semantic accuracy, a worker hard-kill test or a production capacity measurement**. Runtime/test hashes are recorded in the adjacent JSON report. Earlier reports retain their historical hashes and measurements rather than being rewritten as results for this increment.

## Next unfinished checkpoint

Automatic graph-only continuation remains unimplemented. Its implementation must separate graph-job identity/lease from the READY document's evidence execution token; persist a compatible source/chunk snapshot; claim only due bounded work; fence checkpoint/projection writes against deletion and superseded jobs; and retain READY query eligibility while waiting or processing. Do not simply enqueue the existing full retry: it changes task tokens and reruns vector indexing. First verify this job path with isolated real PostgreSQL and separate processes, then hard-kill recovery and cross-store consistency before claiming Phase 3 complete.
