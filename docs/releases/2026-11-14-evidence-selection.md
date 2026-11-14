# Evidence selection release — 14 November 2026

Scope: publish the accumulated, previously reviewed evidence tracing and guarded
supporting-context work to the existing free ConceptGraph Render services. No
private workspaces, graph-worker flag activation, environment changes, paid
resources, schema/data migration or hosted AI evaluation.

Fresh release checks:

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<checkout> <checkout>/.venv/bin/python -m unittest discover -s <checkout>/tests`
  from `/private/tmp`: **701 pass**, 5.056 seconds.
- `npm run test:frontend`: **9 pass**, no failures.
- `npm run build`: TypeScript/Vite **pass**; Vite 6.62 seconds.
- Reviewed source hashes still match `evaluation/supporting-evidence-current.json`.
- `git diff --check`: pass. New fixture/report files contain authored educational
  content, not private PDFs, credentials or interview material.
- Requested authenticated, read-only Qdrant `GET /collections`: **HTTP 200**,
  status `ok`, two collections. This records the successful request, not a
  guarantee about the provider's future inactivity policy.

Deployment verification is performed against the exact published revision:
GitHub Verify status, Render readiness/dependencies, saved public sample,
unauthenticated request rejection and rendered frontend. No live model calls or
production fixture uploads/deletions are authorized by this release.

Measured source selection remains 34/35 on the original authored benchmark,
versus 31/35 before supporting context. The later heldout stayed at 4/5 required
source completeness and 2/3 refusals; no unseen-question improvement or generated
answer accuracy is claimed. The remaining rollback-source miss, missing-attribute
false admissions, supported holdout refusal and offline failed-attempt counter
limitation are documented in `evaluation/supporting-evidence-current.md`.

An eligible already-supported question may make one additional reranking request;
original citations, refusal/identifier guards, score and five-source cap remain
protected. Shared request/token reservation and optional-error fallback remain in
place. Source/ingestion/deletion lifecycle and READY document scope are unchanged.
