# Essential backend closeout before answer-quality work

User priority: finish necessary backend reliability first, then focus retrieval.
Do not pursue optional phases4–8 features or caching/capacity tuning in this pass.
Preserve prior local release notes and all production flags. Offline AI only.

1. TDD safety contracts for a private graph runtime fixture.
2. Exercise actual worker.run_worker and combined ASGI lifespan in separate OS
   processes with real PostgreSQL/Neo4j, saved authored graph drafts/checkpoints,
   forced termination and natural lease recovery. Bound child waits/cleanup.
   Qdrant startup validation/retention timer are explicitly omitted in this graph
   fixture; verify four-store ingestion/deletion separately using existing runner.
3. Run backend/frontend/build and owned store matrices; one fresh final review.
4. Record exact results/remaining limits. Keep production automatic processing
   disabled; no capacity, semantic accuracy or hosted R2 claim. Answer retrieval
   becomes the next development priority, not optional infrastructure expansion.

Verification ledger (ongoing): missing fixture module RED→GREEN. One fresh final
read-only review found a report-scope defect, not a runtime safety defect. Added
RED→GREEN recovery_limits contract and corrected direct-publisher versus natural
runtime lease descriptions; no second review. Final graph-runtime26 checks pass
(232.280s, not capacity); four subprocesses, two SIGKILL/natural-final-lease paths.
Existing PostgreSQL48 regressions pass (8.9929s). All25+18 recorded source hashes
match and compile. Backend677 (8.511s), frontend9 and TS/Vite build (18.04s) pass.
An initial Docker-stopped run failed before resource qualification; started the
existing engine and reran. Final four-store matrix84 passes (224.076s); its47 source hashes match/compile.
All planned closeout checks complete locally; source graph matrix26+PostgreSQL48
retain their separate scopes. No runtime production changes/deployment needed. Saved automation
remains PAUSED and was updated to answer-quality priority. No hosted calls,
production activation or paid services. Ruling: optional capacity/caching/OCR/
revision features are not prerequisites for moving to retrieval quality.
