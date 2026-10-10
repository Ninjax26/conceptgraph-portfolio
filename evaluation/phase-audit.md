# Phase-by-phase release audit

Checked against the local working tree on 10 October 2026. This is a release gate, not a claim that all phases are deployed.

| Phase | Evidence checked | Status and limits |
| --- | --- | --- |
| 1 — Graph reliability | Graph recovery, schema/source validation, allowed relationship types, empty/failed batch distinction, failover repair bounds, resumable checkpoints; full regression suite | Automated checks pass. No live-provider quota/failover run performed in this audit. |
| 2 — OCR | Native/OCR quality-selection regressions, image-region supplementation, orientation, confidence validation, timeout/render caps; reran eight authored English fixtures | All eight current fixture outputs have zero CER/WER under the defined normalization. This does not establish real-document, multilingual, table, handwriting, or LaTeX accuracy. |
| 3 — Retrieval | READY-only scope, bounded candidates, identifier enrichment/verification, finite evidence scores, citations and graph fallback; reran 10/50/100-PDF local retrieval profiles | Current identifier-verified profile accepted all required sources for 29/35 supported questions and refused 10/10 unsupported questions at every size. Remaining supported-question misses are visible in the report. No hosted ANN or generated-answer quality claim. |
| 4 — Public demo | Both saved manifests, source pages/paragraphs, PDF byte hashes, image-only fixture, explicit saved/illustrated labeling and refusal with no invented graph | 24/24 source pages passed native/OCR verification. Examples are authored saved output, not live model results. |
| 5 — Scaling | Configurable resource admission, actual chunk reconciliation, separate worker roles, shared provider budgets, keyset paging, bounded graph display, restart/fencing/cleanup | 297 backend tests, eight frontend status tests, and TypeScript/frontend build pass. Refreshed isolated PostgreSQL 10/50/100-PDF report passes all consistency checks. Real-store hard-kill testing remains unverified. |
| 6 — Private workspaces | Scope reviewed and deliberately postponed | **Deferred at the user's request.** Unfinished workspace files/fields were removed before any migration or data reassignment. Authentication and uploads remain the shared reviewer demo; no owner code is required or used. |

## Live graph diagnostic

A read-only probe of the selected local Neo4j instance counted 271 total nodes. The existing ADA READY-document scope returned zero eligible concepts/relationships; the graph retrieval call completed in 1.407 seconds. This confirms that PostgreSQL graph counts alone do not prove that the selected Neo4j instance has the matching projection. The earlier five-second query timeout is still a recorded failure; this single warm probe does not prove it cannot recur or identify its root cause conclusively.

The local launcher intentionally selects local Neo4j while other stores retain their existing configuration. It does not copy graph data from the previously configured instance. No retained document was automatically rebuilt or migrated as part of this audit.

## Current rollout checks

1. Retain the shared reviewer access contract and clear shared-workspace labels. Private isolation is future work, not a release claim.
2. Preserve existing jobs/checkpoints, document IDs, storage keys and compensating cleanup; no private ownership migration is included.
3. Use the existing free combined Render service, as selected by the user. Enable shared application provider budgets and resource-based admission through its Blueprint; do not create a paid worker.
4. Verify the deployed revision, required-service readiness, protected endpoints, public fixtures and controlled live operations. The local benchmark does not replace deployment checks.

## Additional reliability changes

- Distinguish Neo4j time-budget exhaustion from other graph failures while retaining vector fallback. Report recorded graph counts with a completely missing Neo4j projection explicitly; this is not a full per-node consistency audit.
- Offload synchronous Qdrant deletion from the event loop and drain blocking storage writes/deletes on cancellation before releasing transaction/lease protection. Remote hard kills remain outside this guarantee.
- Count missing legacy source-byte and chunk-reservation metadata as unknown inventory, not just missing pages.
- Reject oversized filenames/course names, including normalization expansion, before storing a PDF or attempting a database insert; these previously could fail tracking after an object write.
- Clear stale ingestion availability after a failed status refresh and ignore status callbacks after dashboard unmount. Preserve fonts, layout, authentication and retrieval/evidence thresholds.
- Add deterministic frontend tests for empty/error graph states and run them in CI.
- Offer consistent local CPU/MPS selection and pin the local launcher to bounded CPU threading. A read-only CPU retrieval/reranking check completed with ten candidates, no accepted evidence and no generation calls in 131.055 seconds under current local load. It does not prove a specific accelerator caused the prior stall; detailed stage timings support further diagnosis.

## Deployment state

This audit was captured before rollout. GitHub authentication is available for the existing `Ninjax26/conceptgraph-portfolio` repository, and the user has signed in to the existing Render dashboard. The original upstream repository remains out of scope. A pre-rollout request to the existing deployed API's public readiness endpoint timed out after 45 seconds with no response, so that check did not confirm production health. Deployment/revision and smoke verification must be checked after rollout; this local audit alone is not deployment proof. No private ownership reassignment or paid-resource creation is included.

## Reproduction

~~~bash
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m compileall -q app scripts tests
npm run build
npm run test:frontend
git diff --check
.venv/bin/python scripts/evaluate_ocr.py
.venv/bin/python scripts/verify_public_samples.py
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python scripts/evaluate_retrieval_scaling.py --documents 10 50 100 --repeats 1
~~~

Reports: [OCR](ocr-quality-current.md), [retrieval](retrieval-scaling-current.md), [ingestion](ingestion-scaling-current.md), [local scaling verification](scaling-validation.md).

Hosting constraint source: [Render's official free-instance documentation](https://render.com/docs/free).
