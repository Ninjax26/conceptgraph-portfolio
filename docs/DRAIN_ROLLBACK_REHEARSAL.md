# Current-code drain and rollback rehearsal

This is a local qualification tool, not an automatic rollout controller. Production
keeps both projection fencing and graph-continuation snapshots disabled.

The experiment runs the actual API and worker entry points against disposable
PostgreSQL, Neo4j, Qdrant and MinIO, with authored graph/answer adapters and fixed
three-dimensional vectors. It consumes no hosted AI quota.

| Cohort | Projection mode | What is checked |
| --- | --- | --- |
| Initial API and two sequential workers | Legacy, disabled | Produce a READY PDF; gracefully drain another worker while its complete vector PUT is held |
| New API and worker | Scoped, enabled locally | Recover the released interrupted lease, preserve the original READY task and legacy point IDs, query/preview sources, delete a third PDF |
| Rollback API and worker | Legacy, disabled | Preserve READY tasks, source access, deletion consistency and durable cleanup obligations |

Before the first transition, API admission is closed and the held worker's actual
coordinator shutdown is observed. The worker and source lease must remain while
the captured request is pending. Only after forwarding and acknowledging the
same complete request, observing worker exit and closing every known actor does
the controller start the scoped cohort. Interrupted ingestion resumes naturally
with a new attempt; this rehearsal does not advance lease or presence clocks.

Stop receipts wrap and await the original coordinator operation. They do not
replace shutdown, retention or resource cleanup logic. Receipt write errors cannot
skip cleanup. API completion requires the actual ASGI shutdown receipt: Uvicorn
then exits with SIGTERM (-15), whereas the worker exits normally (0).

After draining the scoped cohort, the controller deliberately replays the saved
known old execution's synthetic points. This is a negative fixture, **not another
late request and not automatic recovery**. Scoped reads still have current
coverage, but a fresh audit is incompatible with broad legacy reads. The
controller removes only that known stale execution under the existing admission
and current-source row locks. Fresh audits of both surviving fixture sources must
then pass before the rollback cohort starts. Cleanup obligations created by the
third source's actual deletion must survive the transition unchanged.

`legacy_read_snapshot_compatible` checks only the bounded audit's metadata shape,
coverage and absence of stale/unknown/untagged/foreign/malformed/duplicate points.
It never discovers actors, proves a stable snapshot under concurrent writers,
verifies drain or authorizes activation. An operator needs separate, complete
inventory, admission shutdown, request accounting and reconciliation for a real
rollout. Unknown and untagged data must not be silently relabelled.

The native parser's existing vector payload names its canonical course UUID
`document_id`. The auditor now checks this actual field. The rehearsal exposed
that the preceding auditor and its synthetic fixtures checked `course_id`, causing
real native parser output to be rejected as foreign. A characterization test using
an actual parser-produced chunk reproduced the failure before the correction.
Parser output, vector schemas, stored payloads and retrieval APIs are unchanged;
no backfill or migration is necessary. Historical reports retain their original
synthetic-test scope and hashes.

Run the owned fixture with a private test socket:

```sh
.venv/bin/python scripts/evaluate_upload_deletion.py \
  --socket-dir <private-test-socket> --projection-fencing --worker-crashes \
  --full-lifecycle --vector-rollout-audit --fleet-inventory --drain-rollback
```

The adjacent [machine-readable report](../evaluation/drain-rollback-current.json)
and [readable report](../evaluation/drain-rollback-current.md) record actual
observations, source hashes and limits. A failed run raises and does not replace
the reports with a success result. Teardown removes only its owned test resources.

Limits: current binaries in legacy/scoped/legacy configuration; seven known child
processes and one deferred PUT; two surviving synthetic sources; controlled exact
task reconciliation; no historical binaries, unknown helpers, arbitrary network
failure, production fleet attestation, hosted R2, vendor capacity, realistic load,
OCR or answer/relationship semantic quality. Global activation remains false.
This advances a Phase 3 checkpoint, not completion of Phase 3 or phases 4–8.
