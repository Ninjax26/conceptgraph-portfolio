# Provider-generated GraphRAG ablation — 9 September 2026

One real run over 22 labelled questions and three authored PDFs. The configured
Groq→Gemini route produced 61 validated concepts and 44 relationships. PDF parsing,
MiniLM embeddings, Qdrant search, Neo4j traversal, cross-encoder reranking, and the
evidence gate used production code; answer synthesis was intentionally excluded.

Graph expansion improved raw all-required-source recall from 16/17 to 17/17 for
both graph modes. It did not improve the final evidence-gated score and added
0.35–0.43 seconds of average retrieval latency. Two-hop terms were used by 3/22
queries. This is a small fixture result, not a claim of general superiority.

| Metric | Vector only | One hop | Two hop |
| --- | ---: | ---: | ---: |
| Correct document in top 5 | 17/17 | 17/17 | 17/17 |
| Primary expected page in top 5 (single-page label) | 15/17 | 15/17 | 15/17 |
| Supported questions with evidence | 17/17 | 17/17 | 17/17 |
| Unsupported questions refused | 5/5 | 5/5 | 5/5 |
| Average retrieval time | 0.06s | 0.48s | 0.41s |
| p95 retrieval time | 0.10s | 1.55s | 0.83s |
| raw vector/graph retrieval — page hits @5 | 17/17 | 17/17 | 17/17 |
| raw vector/graph retrieval — all required sources @5 | 16/17 | 17/17 | 17/17 |
| after cross-encoder reranking — page hits @5 | 17/17 | 17/17 | 17/17 |
| after cross-encoder reranking — all required sources @5 | 17/17 | 17/17 | 17/17 |
| after evidence gate — page hits @5 | 17/17 | 17/17 | 17/17 |
| after evidence gate — all required sources @5 | 12/17 | 12/17 | 12/17 |
| request errors | 0 | 0 | 0 |
| graph→vector fallbacks | 0 | 0 | 0 |
| queries with two-hop terms | 0 | 0 | 3 |
