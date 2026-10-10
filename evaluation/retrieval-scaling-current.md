# Retrieval scaling check

Authored PDFs with synthetic distractors; warm local models and in-memory Qdrant brute-force search.

| PDFs | Chunks | Profile | Reranked all sources @5 | Accepted all sources @5 | Refusals | Reranked MRR@5 | p50 seconds | p95 seconds |
| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 32 | dense_10 | 35/35 | 29/35 | 7/10 | 0.9857 | 0.037 | 0.053 |
| 10 | 32 | dense_20 | 35/35 | 29/35 | 7/10 | 0.9857 | 0.076 | 0.091 |
| 10 | 32 | identifier_20 | 35/35 | 29/35 | 7/10 | 0.9857 | 0.077 | 0.102 |
| 10 | 32 | identifier_10_verified | 35/35 | 29/35 | 10/10 | 0.9857 | 0.038 | 0.058 |
| 50 | 112 | dense_10 | 34/35 | 28/35 | 7/10 | 0.9857 | 0.037 | 0.067 |
| 50 | 112 | dense_20 | 34/35 | 28/35 | 7/10 | 0.9857 | 0.067 | 0.114 |
| 50 | 112 | identifier_20 | 35/35 | 29/35 | 7/10 | 0.9857 | 0.07 | 0.108 |
| 50 | 112 | identifier_10_verified | 35/35 | 29/35 | 10/10 | 0.9857 | 0.038 | 0.061 |
| 100 | 212 | dense_10 | 34/35 | 28/35 | 7/10 | 0.9857 | 0.04 | 0.055 |
| 100 | 212 | dense_20 | 34/35 | 28/35 | 7/10 | 0.9857 | 0.077 | 0.098 |
| 100 | 212 | identifier_20 | 35/35 | 29/35 | 7/10 | 0.9857 | 0.075 | 0.1 |
| 100 | 212 | identifier_10_verified | 35/35 | 29/35 | 10/10 | 0.9857 | 0.041 | 0.061 |

## Limits

- No hosted ANN, HTTP, concurrency, PostgreSQL, or LLM generation benchmark.
- Page-label precision is not human-verified passage relevance or generated-answer accuracy.
- Source fragments are checked by exact text presence, not semantic correctness.
- Validation cases were used to diagnose identifier failures; fresh held-out authored cases are separate and not independently annotated.
- One warm sequential run per case; not a concurrency, uptime, or production-scale guarantee.
