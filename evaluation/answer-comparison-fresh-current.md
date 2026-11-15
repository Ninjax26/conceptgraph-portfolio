# Phase 2 offline quality checks

Output kind: `live_groq_oracle_synthesis_fresh_candidate`. Semantic metrics use reviewed records only.

| Check | Actual result |
| --- | ---: |
| Captured responses | 8/8 |
| Citation labels with invalid indices | 0/8 |
| Labels citing the wrong page | 0/8 |
| Literal source excerpts | 10/10 |
| Hash-bound human answer reviews | 0/8 |
| Known prerequisite links found | 6/6 |
| Graph edges pending semantic review | 44/44 |
| Groundedness (reviewed claims only) | Not measured (review pending) |
| Citation correctness (reviewed claims only) | Not measured (review pending) |
| Mean completeness (0 reviewed supported answers) | Not measured (review pending) |
| Refusal correctness | Not measured (review pending) |

Missing reviews receive **no semantic score**, not zero or assumed success. Partial reviewed metrics are not whole-dataset accuracy.

## Limits

- No live provider generation, production traffic or data-store access.
- Fallback output is not LLM answer quality; context is oracle-selected, not retrieved.
- Citation format and literal excerpts do not prove claim support.
- Semantic answer metrics require explicit hash-bound human reviews; missing reviews are not zero scores.
- Graph snapshot is an older saved provider extraction, not a new run of the current extractor.
- Partial gold reference measures six known links, not total graph semantic precision/recall.
