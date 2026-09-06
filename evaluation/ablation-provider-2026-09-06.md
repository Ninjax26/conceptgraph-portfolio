# Provider-generated graph ablation — 6 September 2026

22 questions, three modes, one repeat (66 retrieval requests). Real PDF parsing, embeddings, vector search, Neo4j traversal, reranking, and evidence filtering; answer synthesis is excluded.

Graph coverage was limited by provider quota: 8 nodes and 7 edges in web-foundations.pdf; no nodes or edges in the other two PDFs. No two-hop expansion terms were used. The equal scores do not establish that two-hop traversal helps or that complete graphs would give the same result. See the JSON for per-question traces and methodology. The separate reference-graph experiment controls extraction quality using authored graphs.

| Metric | Vector only | One hop | Two hop |
| --- | ---: | ---: | ---: |
| Correct document in top 5 | 17/17 | 17/17 | 17/17 |
| Primary expected page in top 5 (single-page label) | 15/17 | 15/17 | 15/17 |
| Supported questions with evidence | 17/17 | 17/17 | 17/17 |
| Unsupported questions refused | 5/5 | 5/5 | 5/5 |
| Average retrieval time | 0.05s | 0.32s | 0.28s |
| p95 retrieval time | 0.05s | 0.60s | 0.40s |
| raw vector/graph retrieval — page hits @5 | 17/17 | 17/17 | 17/17 |
| raw vector/graph retrieval — all required sources @5 | 16/17 | 16/17 | 16/17 |
| after cross-encoder reranking — page hits @5 | 17/17 | 17/17 | 17/17 |
| after cross-encoder reranking — all required sources @5 | 17/17 | 17/17 | 17/17 |
| after evidence gate — page hits @5 | 17/17 | 17/17 | 17/17 |
| after evidence gate — all required sources @5 | 13/17 | 13/17 | 13/17 |
| request errors | 0 | 0 | 0 |
| graph→vector fallbacks | 0 | 0 | 0 |
| queries with two-hop terms | 0 | 0 | 0 |
