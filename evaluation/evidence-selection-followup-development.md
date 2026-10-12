# Retrieval scaling check

Authored PDFs with synthetic distractors; warm local models and in-memory Qdrant brute-force search.

| PDFs | Chunks | Profile | Reranked all sources @5 | Accepted all sources @5 | Refusals | Reranked MRR@5 | p50 seconds | p95 seconds |
| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 32 | identifier_10_verified | 27/27 | 23/27 | 8/8 | 0.9815 | 0.068 | 0.115 |
| 50 | 112 | identifier_10_verified | 27/27 | 23/27 | 8/8 | 0.9815 | 0.068 | 0.123 |
| 100 | 212 | identifier_10_verified | 27/27 | 23/27 | 8/8 | 0.9815 | 0.067 | 0.156 |

## Separate dataset splits

| PDFs | Profile | Split | Accepted all required sources | Unsupported refusals |
| ---: | --- | --- | ---: | ---: |
| 10 | identifier_10_verified | development | 14/17 | 5/5 |
| 10 | identifier_10_verified | validation | 9/10 | 3/3 |
| 50 | identifier_10_verified | development | 14/17 | 5/5 |
| 50 | identifier_10_verified | validation | 9/10 | 3/3 |
| 100 | identifier_10_verified | development | 14/17 | 5/5 |
| 100 | identifier_10_verified | validation | 9/10 | 3/3 |

## Limits

- No hosted ANN, HTTP, concurrency, PostgreSQL, or LLM generation benchmark.
- Page-label precision is not human-verified passage relevance or generated-answer accuracy.
- Source fragments are checked by exact text presence, not semantic correctness.
- Validation cases were used to diagnose identifier failures; fresh held-out authored cases are separate and not independently annotated.
- One warm sequential run per case; not a concurrency, uptime, or production-scale guarantee.
