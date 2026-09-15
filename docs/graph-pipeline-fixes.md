# Graph pipeline corrections

## Confirmed bugs fixed

1. **IDs collided across extraction batches.** Providers may assign `0` or `1`
   independently in every response. The merger now assigns a deterministic unused
   ID when different concept names collide and remaps relationship endpoints.
   Matching normalized names continue to merge into one concept.
2. **Relationship recovery prompt contradicted validation.** Recovery now explicitly
   requests nodes as well as relationships, including every relationship endpoint.
3. **Single-chunk documents skipped relationship recovery.** A validated graph with
   two or more concepts but insufficient relationships can now use its one source
   chunk for the existing bounded recovery pass.
4. **Recovery success did not require new relationships.** The success flag now
   requires an increase in validated, deduplicated relationships.
5. **Timeout/quota skips were mislabelled as demo budget limits.** The budget flag
   now records the configured batch cap independently of later provider failures.

6. **Retry eligibility blocked corrected recovery.** Single-chunk documents with
   multiple concepts remain eligible. Version 2 recovery results can use the
   corrected version 3 pass, within the existing attempt limit.

## Verification

Seven regression tests cover ID collisions, name merging, single-chunk recovery,
empty recovery results, timeout accounting, and genuine batch limits. The full
175-test Python suite passes, including existing OCR, processing, provider failover,
retrieval-mode, and security tests. These are automated tests with mocked service
boundaries; they do not establish live provider accuracy or complete PDF coverage.

The readiness endpoint reports the Render commit revision so deployments can be
verified without relying only on a successful health response.

## Existing documents and limits

Deploying does not regenerate stored graphs. Use the existing graph retry/rebuild
workflow to apply the fixes to a partial document. Valid extraction checkpoints
remain reusable. No synthetic relationships are inserted to make a graph look
connected. Missing evidence, extraction limits, provider quotas, and timeouts can
still produce partial graphs.
