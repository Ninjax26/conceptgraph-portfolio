# Ingestion scaling check

Isolated ASGI upload/delete endpoints, local PostgreSQL leases/checkpoints, cached MiniLM CPU embeddings and in-memory Qdrant. Sources and graph projection are test doubles; graph LLM requests are disabled.

| PDFs | Pages | Chunks | Source bytes | Workers | Total ingestion s | Queue p95 s | Indexing sum s | Retrieval p95 s | Graph calls |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 20 | 20 | 19554 | 2 | 30.333 | 17.748 | 10.972 | 0.3509 | 0 |
| 50 | 100 | 100 | 97763 | 2 | 101.913 | 62.141 | 21.336 | 0.1217 | 0 |
| 100 | 200 | 200 | 195581 | 2 | 204.029 | 87.813 | 53.374 | 0.5928 | 0 |

## Checks

- concurrent_schema_initialization: passed
- ready_scope_10: passed
- pagination_10: passed
- cleanup_10: passed
- ready_scope_50: passed
- pagination_50: passed
- cleanup_50: passed
- ready_scope_100: passed
- pagination_100: passed
- cleanup_100: passed
- restart_resumed_checkpoint: passed
- superseded_attempt_cannot_complete: passed
- shared_provider_atomic_budget: passed

## Limits

- Worker coordinators share a Python process but use separate identities and concurrent PostgreSQL transactions.
- Restart is graceful cancellation and durable checkpoint recovery, not a hard-kill test of remote in-flight writes.
- No live Neo4j, R2, provider latency, hosted ANN, TCP or production load is measured.
- Source storage is exact fixture bytes; vector bytes are a float32 estimate, not full persisted-index overhead.
- Five warm vector-only retrieval samples per corpus; no reranking or generation performance claim.
