# Retrieval scaling check

Authored PDFs with synthetic distractors; warm local models and in-memory Qdrant brute-force search.

| PDFs | Chunks | Profile | Reranked all sources @5 | Accepted all sources @5 | Refusals | Reranked MRR@5 | p50 seconds | p95 seconds |
| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 32 | identifier_10_verified | 6/6 | 4/6 | 2/4 | 0.9167 | 0.156 | 0.249 |
| 50 | 112 | identifier_10_verified | 6/6 | 4/6 | 2/4 | 0.9167 | 0.173 | 0.207 |
| 100 | 212 | identifier_10_verified | 6/6 | 4/6 | 2/4 | 0.9167 | 0.115 | 0.671 |

## Separate dataset splits

| PDFs | Profile | Split | Accepted all required sources | Unsupported refusals |
| ---: | --- | --- | ---: | ---: |
| 10 | identifier_10_verified | development | 2/3 | 1/2 |
| 10 | identifier_10_verified | held_out | 2/3 | 1/2 |
| 50 | identifier_10_verified | development | 2/3 | 1/2 |
| 50 | identifier_10_verified | held_out | 2/3 | 1/2 |
| 100 | identifier_10_verified | development | 2/3 | 1/2 |
| 100 | identifier_10_verified | held_out | 2/3 | 1/2 |

## Limits

- No hosted ANN, HTTP, concurrency, PostgreSQL, or LLM generation benchmark.
- Page-label precision is not human-verified passage relevance or generated-answer accuracy.
- Source fragments are checked by exact text presence, not semantic correctness.
- Validation cases were used to diagnose identifier failures; fresh held-out authored cases are separate and not independently annotated.
- One warm sequential run per case; not a concurrency, uptime, or production-scale guarantee.
