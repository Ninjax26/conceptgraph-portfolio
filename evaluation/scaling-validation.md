# Phase 5 local verification

Verified on 10 October 2026. Changes are local; this is not a production deployment report.

## Automated checks

| Command | Result |
| --- | --- |
| `.venv/bin/python -m unittest discover -s tests -q` | 299 tests passed after the OCR threading regression checks |
| `npm run test:frontend` | 8 tests passed |
| `.venv/bin/python -m compileall -q app scripts tests` | Passed |
| `npm run build` | TypeScript check and Vite build passed |
| `git diff --check` | Passed |
| Impeccable detector on changed dashboard/API/graph-diagnostic frontend files | Passed, no findings |
| `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python scripts/evaluate_ingestion_scaling.py --documents 10 50 100 --workers 2` | Passed; [full measured report](ingestion-scaling-current.md) |

The PostgreSQL-backed fixture run checked concurrent schema initialization, READY-only eligibility, canonical keyset pagination, deletion cleanup at each corpus size, graceful checkpoint recovery, superseded-attempt fencing, and atomic shared-provider request reservations. The refreshed 100-PDF fixture corpus reached READY in 204.029 seconds with zero duplicate executions. It ran more slowly than the earlier check; no isolated-machine or speed-improvement claim is made.

The metadata-inventory tests verify dry-run behavior, transaction/row locking for apply, skipping records that become active, and unavailable source handling. They do not apply inventory updates to retained user documents.

## Local application

- API: `http://127.0.0.1:8001`, running with `PROCESSING_ROLE=api`.
- Ingestion: a separate worker process, with shared PostgreSQL provider budgets enabled.
- Frontend: `http://127.0.0.1:5174/dashboard`.
- Readiness reported `status=ready`, `active_workers=1`, `ingestion_available=true`, `graph_available=true`, and no degraded services.
- The authenticated dashboard loaded eight retained uploads from the paginated endpoint. Previous and Next were correctly disabled for this single-page list. Larger-corpus pagination was exercised through the real ASGI endpoint in the fixture benchmark, not through a browser Next/Previous interaction.
- Desktop (1440px) and mobile (390px) checks found no horizontal document overflow. The browser console reported no warnings/errors during this check. Existing fonts and layout were retained.
- A question entered by the user during verification exposed a live Neo4j retrieval timeout. The API logged fallback to vector retrieval; a successful live graph answer was not established. Connectivity readiness is not proof that every graph query completes within its timeout.

## Not established by these checks

- Real Neo4j/R2 deletion consistency under abrupt process termination: the benchmark uses source/projection test doubles.
- Live generation throughput or provider-quota exhaustion: benchmark generation is disabled; graph calls are zero.
- Hosted ANN, reranking, generated-answer latency, long/scanned-PDF capacity, or multi-process memory capacity: retrieval uses five warm vector-only samples over small two-page fixture PDFs.
- Private workspace isolation: Phase 6 is explicitly deferred at the user's request; reviewer uploads remain shared. The unfinished workspace files/fields were removed before any schema initialization; no workspace migration or data reassignment was run.
- Production rollout: no commit, push, deployment, credentials update, or production migration was performed.
