# Ingestion scaling check

Isolated ASGI upload/delete endpoints, local PostgreSQL leases/checkpoints, cached MiniLM CPU embeddings and in-memory Qdrant. Sources and graph projection are test doubles; graph LLM requests are disabled.

| PDFs | Pages | Chunks | Source bytes | Workers | Total ingestion s | Queue p95 s | Indexing sum s | Retrieval p95 s | Graph calls |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 4 | 4 | 3906 | 2 | 0.871 | 0.144 | 0.217 | 0.0344 | 0 |

## Checks

- concurrent_schema_initialization: passed
- pagination_2: passed
- cleanup_2: passed
- restart_resumed_checkpoint: passed
- shared_provider_atomic_budget: passed

## Limits

- Worker coordinators share a Python process but use separate identities and concurrent PostgreSQL transactions.
- Restart is graceful cancellation and durable checkpoint recovery, not a hard-kill test of remote in-flight writes.
- No live Neo4j, R2, provider latency, hosted ANN, TCP or production load is measured.
- Source storage is exact fixture bytes; vector bytes are a float32 estimate, not full persisted-index overhead.
- Five warm vector-only retrieval samples per corpus; no reranking or generation performance claim.
