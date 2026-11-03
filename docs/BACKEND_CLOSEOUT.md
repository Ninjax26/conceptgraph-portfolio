# Essential backend closeout before answer quality

The user's 3 November priority is answer retrieval. Optional corpus capacity,
new infrastructure, OCR expansion and revision-comparison features are deferred;
they are not prerequisites for improving answers.

The existing graph-only processor, idle scheduler and recovery/retention services
are already implemented. This pass adds a private subprocess qualification, not
another production pipeline or a change to API/auth/READY/storage behavior.

The graph-runtime mode includes all prior publication, one-claim, idle-dispatch
and maintenance cases, then runs actual `app.worker.run_worker` and the combined
`app.main.lifespan` in four OS processes. Each role is killed after its real Neo4j
commit and before SQL receipt completion. A new process waits for the natural
60-second final lease expiry; the gated maintenance path observes the exact
marker and completes the receipt/result without extraction or a second remote
write. Original READY task identity, three chunks and checkpoints are preserved.
Children have bounded waits and graceful SIGTERM cleanup; SIGKILL is deliberate.
Earlier quota fixtures are explicitly stopped to isolate the long waiting test.

Important limits: this graph fixture uses authored saved drafts and PostgreSQL/
Neo4j only. Qdrant startup validation, the retention timer and global close hooks
are replaced with no-ops, and there is no HTTP server. Five older direct-publisher
crash schedules still advance their fixture lease clocks; the new two runtime
cases do not. This does not demonstrate all crash boundaries, hosted AI quality,
production capacity, hosted R2 or full four-store graph-worker operation. The
existing separate four-store matrix exercises ingestion/deletion/lifecycle/drain
and rollback; it must not be conflated with this graph-specific fixture.

Commands (all store fixtures use a new owned private PostgreSQL socket):

```sh
.venv/bin/python -m unittest discover -s tests -v
npm run test:frontend
npm run build
.venv/bin/python scripts/evaluate_graph_publication_recovery.py --socket-dir <owned-socket> --graph-runtime
.venv/bin/python scripts/evaluate_graph_continuation_jobs.py --socket-dir <owned-socket>
.venv/bin/python scripts/evaluate_upload_deletion.py --socket-dir <owned-socket> --projection-fencing --worker-crashes --full-lifecycle --vector-rollout-audit --fleet-inventory --drain-rollback
```

Keep automatic graph/projection flags off in production; deployment activation is
separate from offline readiness to work on answer retrieval. Do not remove any
safety protocol or user isolation while improving evidence selection.

Next product work: trace the four original source misses and known-identifier
missing-attribute refusals through search, reranking, passage selection and the
evidence gate. Establish separate development/held-out evidence measures first.
Use cached local models/authored data only until a hosted quota is approved.

Final results: **677 backend tests**, **nine frontend tests**, production build,
**26 graph runtime checks**, **48 PostgreSQL regressions** and **84 four-store
checks** pass. Graph/store fixture times232.280/224.076 seconds are not capacity.
All25+18+47 source hashes match and compile; diff checks pass. One reviewed
report-scope defect was fixed with RED→GREEN coverage. Owned resources removed;
zero hosted AI calls or production mutations. These changes remain local and
production gates are unchanged. The next development task is evidence selection,
with concrete known failures documented in [ANSWER_QUALITY_NEXT.md](ANSWER_QUALITY_NEXT.md).
