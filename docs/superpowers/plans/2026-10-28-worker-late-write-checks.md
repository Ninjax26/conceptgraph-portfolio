# Killed worker and delayed vector request qualification

Use superpowers:executing-plans inline, then one fresh-context read-only review.

## Ruling and scope

Continue from `4009f78`. Test actual coordinator/processor processes, real isolated PostgreSQL/Qdrant/Neo4j/MinIO and a loopback HTTP delay proxy. Hold a complete vector PUT before forwarding it, kill its worker with SIGKILL, then deliver the accepted request after recovery or completed deletion. Fixed vectors and authored graph output replace inference; native PDF parsing and store requests remain real. No new runtime hooks, dependencies, schemas, production data or environment changes are needed for this qualification.

A direct point replay is insufficient: it does not demonstrate process death releasing source locks while a remote request remains outstanding. The proxy must preserve the accepted request body, validate its source/task and private collection, forward only to loopback, and drain/close all owned resources. Lease expiry is advanced only for the owned fixture rather than waiting minutes; the fresh coordinator performs the actual task rotation and claim.

## Tasks

- [x] Eight offline proxy contracts cover delayed/disconnected delivery, exact source scope, loopback-only targets, shutdown, malformed/foreign requests, the installed SDK/catalog read, and raw/encoded traversal rejection.
- [x] Two real coordinator SIGKILLs release source locks; two fresh coordinators classify old attempts and recover attempt 2 to READY. Old delivery cannot overwrite current evidence or enter scoped retrieval.
- [x] Deliver the accepted request after valid recovered-READY authenticated DELETE; measure every store and run existing cleanup in a fresh process.
- [x] One vector survives the fresh runtime sweep. Report overall FAIL/exit 1, without counting explicit manual fixture cleanup as application recovery.
- [x] 18 original deletion plus 10 processor regressions, seven worker control assertions, 591 backend tests, four-file compile/diff, 22 current report hashes and owned fixture cleanup. Frontend unchanged; no fresh frontend build, with the prior release's results retained only as history.
- [x] One fresh read-only review; fix its path-scope finding with a RED/GREEN regression and narrow the READY claim. Re-run the complete fixture and final suite after fixes. Keep flags off and failed qualification local: no commit/push/deploy.

Final report: [worker-crash-current.md](../../../evaluation/worker-crash-current.md), with adjacent JSON. Qualification fails solely at the measured post-delete vector recovery criterion; other assertions pass. Runtime/schema/environment are unchanged. Next work is durable late-vector cleanup, including restart, isolation and a write arriving after a previous successful/empty sweep. A one-shot zero-vector observation cannot establish that no accepted write is still outstanding.

## Acceptance

Both restart and deletion must be safe before activation. A failing deletion experiment is useful evidence, not a passing safety test: write a separate failed qualification report and keep the earlier report historical. Avoid inventing a new cross-store protocol until this experiment identifies the boundary that needs it. The coordinator subprocess is not the complete `app.worker` lifecycle, separate API deployment, production worker presence or a scaling benchmark.
