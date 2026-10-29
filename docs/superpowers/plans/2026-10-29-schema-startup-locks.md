# Avoid unnecessary startup DDL locks

Use executing-plans inline, TDD and a fresh final read-only review.

**Observed failure:** The final rollout qualification hit SQLSTATE `40P01` during concurrent recovery startup. The schema advisory lock serializes initializers but does not cover existing recovery readers. Repeated `ALTER ... ADD COLUMN IF NOT EXISTS` still takes exclusive relation locks even when columns exist. A reader holds publication-intent access and then requests document access while initialization holds documents and requests publication intents.

**Reproduction:** `scripts/schema_startup_checks.py` holds a real ordinary reader open and observes actual document-table exclusive locks. A second joined read reproduces `40P01` in the owned PostgreSQL fixture. No mocked SQL or retry hides this failure.

**Ruling:** Skip additive column DDL only after inspection confirms the column already exists. Preserve exact existing column definitions, schema advisory lock, transaction, index statements and data migrations. No new migration, schema or API is introduced. Actual missing-column upgrades can still need a coordinated maintenance window; this fix does not certify arbitrary rolling DDL.

- [x] RED: direct helper contracts for existing/missing columns and same-transaction execution; controlled real PostgreSQL deadlock reproduction.
- [x] GREEN: reuse one private column-inspection helper for document/attempt/publication/provider-budget additive columns. Keep all other initialization behavior.
- [x] Verify current-schema startup with an open reader, concurrent recovery, and the existing legacy provider-budget additive/reinitialization suite. Run full backend, frontend/build and exact report hashes.
- [x] Fresh read-only review of this additional runtime fix, separate from the already-reviewed metadata auditor. Record actual evidence, boundaries and release revision.

Local verification complete: combined report passes 68 checks, including 10 vector-audit cases and 1 controlled current-schema startup case, with 41 matching source hashes/compile checks and owned resource removal. 621 backend tests, nine frontend tests, TypeScript/Vite build and diff pass. Separate legacy provider-budget PostgreSQL suite passes 10 checks. Auditor review found malformed direct contract construction; its RED/GREEN regression and strict validation fix are included. A separate startup-fix review found no blockers. No activation, hosted AI or production test fixture. Release verification is recorded separately in the main ledger.
