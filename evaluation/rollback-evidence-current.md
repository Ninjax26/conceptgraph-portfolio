# Rollback evidence and generation prompt

The known development case q13 asks why a PostgreSQL rollback cannot remove partial Qdrant writes. The baseline retrieved both required pages from `data-reliability.pdf`. It cited page 4 for the Qdrant limitation and cleanup. The original rerank on page 1 scored **0.0978** after combining it with vector similarity, below the **0.35** evidence threshold.

The existing optional second pass now recognizes narrow database rollback questions. It asks about writes outside the original transaction and uses the same already scoped candidates. Page 1 scored **0.4629** on that follow-up. Page 4 remained the first citation and page 1 followed it. The prompt still uses the original question. It now asks the model to explain the cause from the sources and keep recovery separate from what the rollback itself does.

| Existing fixture split | All required pages before | All required pages after | Unsupported refusals before and after |
| --- | ---: | ---: | ---: |
| Development | 16/17 | 17/17 | 5/5 |
| Validation | 10/10 | 10/10 | 3/3 |
| Previously used held-out split | 8/8 | 8/8 | 2/2 |
| Total | 34/35 | 35/35 | 10/10 |

The two replays used identical corpus and question hashes, 10 PDFs, 32 chunks, in-memory Qdrant, and cached MiniLM embedding and reranking models. The final replay had 45 cases and zero request errors. q13 was the only case whose citation pages changed. The original refusal decision, primary citation order, and evidence score were preserved in all 45 cases. The two reviewed source passages can reach the actual synthesis prompt, which the regression checks offline. The focused pass can use one additional rerank request on matching production questions. It does not use an extra generation request.

Baseline code was commit `8f409204e31adbfd721bbacdfd90eee074cd8763`. The current `evidence_facets.py` SHA-256 was `3480c6d3e3ec022996e76a5b3f20803e8ab8f4ffae318a06fa5173af93e538b7`. Run on each code version with a distinct output path.

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python scripts/evaluate_retrieval_scaling.py \
  --documents 10 --profiles identifier_10_verified --trace-evidence --output /private/tmp/cg-retrieval-check
.venv/bin/python -m unittest discover -s tests -q
```

804 backend tests passed. These results measure source-page coverage on previously used authored questions. Hosted reranking and generated answer quality on fresh examples remain unmeasured.
