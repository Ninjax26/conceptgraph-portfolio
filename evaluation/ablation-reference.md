| Metric | Vector only | One hop | Two hop |
| --- | ---: | ---: | ---: |
| Correct document in top 5 | 51/51 | 51/51 | 51/51 |
| Primary expected page in top 5 (single-page label) | 45/51 | 45/51 | 45/51 |
| Supported questions with evidence | 51/51 | 51/51 | 51/51 |
| Unsupported questions refused | 15/15 | 15/15 | 15/15 |
| Average retrieval time | 0.05s | 0.26s | 0.28s |
| p95 retrieval time | 0.08s | 0.39s | 0.39s |
| raw vector/graph retrieval — page hits @5 | 51/51 | 51/51 | 51/51 |
| raw vector/graph retrieval — all required sources @5 | 48/51 | 51/51 | 51/51 |
| after cross-encoder reranking — page hits @5 | 51/51 | 51/51 | 51/51 |
| after cross-encoder reranking — all required sources @5 | 51/51 | 51/51 | 51/51 |
| after evidence gate — page hits @5 | 51/51 | 51/51 | 51/51 |
| after evidence gate — all required sources @5 | 39/51 | 39/51 | 39/51 |
| request errors | 0 | 0 | 0 |
| graph→vector fallbacks | 0 | 0 | 0 |
| queries with two-hop terms | 0 | 0 | 36 |
