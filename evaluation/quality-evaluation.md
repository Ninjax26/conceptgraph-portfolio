# Phase 2 — answer, citation and graph quality

## Scope

A small offline evaluator adds twelve authored answer cases (nine supported, three unsupported), explicit claim checklists, six source-backed prerequisite references, and hash-bound review worksheets. It does not introduce an LLM judge, a new model, new dependencies, database writes or production data collection.

The user selected **offline checks only**. Accordingly, no new Groq, Gemini, Cohere or graph-extraction request was made. The measured response path is the application's actual deterministic evidence fallback, explicitly labelled **not LLM output**. Graph checks use the older saved fixture extraction, not a fresh test of today's extractor.

## What is measured automatically

- Citation labels parse correctly and refer to a real source index.
- An explicitly cited page matches that source's page.
- Supplied excerpts literally occur on their declared source pages. Authored computing context is verified against PDF bytes/text; graph snapshots must match source-byte hashes.
- Raw graph endpoints, all six allowed relationship types, duplicate node IDs/edges, self-edges and provenance metadata are audited without first dropping invalid relations.
- Known prerequisite reference links are matched by type and direction. `RELATED_TO` is symmetric; other relations are directional. Unlabelled edges are **not** automatically wrong.

## What requires review

Each answer review lists **every factual claim**, whether it is supported, whether its citation is correct, and which expected claims are covered. Unsupported-question refusal correctness is reviewed independently of whether relevant-but-nonanswering sources were provided. Mere citation presence or keyword overlap is not sufficient.

Semantic review must name a reviewer and match hashes of the answer/source list, the case/checklist and full source-page context. Changed answers, source snippets, context or labels invalidate old reviews. Incomplete/unreviewed records receive `null` semantic scores, not assumed successes or zero-accuracy scores.

Graph reviewers assess support and direction against literal source quotes. Reviews bind to the exact extraction snapshot. Full graph semantic precision stays unmeasured until every edge is reviewed; the partial six-link gold reference is not a complete graph accuracy dataset.

## Actual offline results

| Check | Result |
| --- | ---: |
| Deterministic fallback captures | 12/12 |
| Valid citation indices | 20/20 |
| Wrong explicit citation pages | 0/20 |
| Literal supplied excerpts | 21/21 |
| Saved graph nodes / edges | 61 / 44 |
| Dangling edges / unsupported types / self-edges / exact duplicates | 0 / 0 / 0 / 0 |
| Nodes with valid provenance metadata | 61/61 |
| Known prerequisite links found | 6/6 |
| Answer semantic reviews | 0/12 — pending |
| Graph edge semantic reviews | 0/44 — pending |
| LLM groundedness / citation correctness / answer completeness / refusal accuracy | Not measured |

The saved graph also contains both **Transactions** and **Database Transaction** matching one annotated prerequisite reference. The evaluator flags this alias-duplicate match; it does not silently merge or rewrite the graph. Valid node provenance metadata does not prove that a particular relationship is supported.

The Phase 1 challenge already shows why these distinctions matter: topic/identifier matches can survive the evidence gate while the requested manufacturer or serial number is absent. A live LLM might refuse correctly, but the offline retrieval score cannot establish that outcome.

## Run and review

```bash
# No provider calls; captures the existing deterministic fallback only.
.venv/bin/python scripts/evaluate_quality.py --fallback

# Score previously captured answer/source JSON. No synthesis occurs here.
.venv/bin/python scripts/evaluate_quality.py --responses evaluation/quality-offline-current.responses.json

# After completing the worksheet with genuine judgments:
.venv/bin/python scripts/evaluate_quality.py --responses evaluation/quality-offline-current.responses.json --reviews evaluation/quality-offline-current.review.json --output evaluation/quality-reviewed
```

The `.review.json` contains editable `answers` and `graph_edges` lists plus their context. For each answer, fill reviewer, claim judgments, expected claim IDs covered and refusal correctness, then set `all_claims_reviewed` only after assessing every factual claim. For each edge, fill reviewer, support/direction judgments and literal evidence objects with document, page and quote. Do not alter the supplied hashes to reuse a stale review.

Existing review files are never overwritten on rerun. Use a new `--output` stem to get a fresh worksheet if answers change. Captures use `{"output_kind": "...", "captures": [{"id": "qa01", "response": {"answer": "...", "sources": [...]}}]}`; duplicate/unknown case IDs are rejected. Keep private captures/reviewer information local unless intentionally sanitized for publication.

Artifacts: [machine report](quality-offline-current.json), [summary](quality-offline-current.md), [fallback captures](quality-offline-current.responses.json), [review worksheet](quality-offline-current.review.json).

## Verification and remaining work

Sixteen quality tests check invalid/zero/malformed citations, wrong pages, fabricated excerpts, unknown semantic scores, completeness, stale/incomplete reviews, refusal with relevant context, reversed prerequisites, symmetric duplicates, unsupported/dangling edges, partial gold references, all six types and graph-review hashes.

The offline tooling and structural baseline are implemented. **Semantic LLM answer evaluation is still pending** actual captured model outputs and completed reviews; semantic quality of all 44 graph edges is also pending. No production-level AI accuracy is claimed. A later capture must respect provider budgets and needs separate approval because the current run is explicitly offline.
