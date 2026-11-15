# Candidate answer comparison: offline preparation

Prepared after release d5626c2. This pack contains the deployed source-faithful
prompt and exact authored oracle passages. No model execution or scoring occurred.

| Group | Cases | Proposed model requests | Maximum completion tokens |
| --- | ---: | ---: | ---: |
| Known questions, matching historical source contexts | 12 | 11 | 13,200 |
| Fresh questions, candidate only | 8 | 8 | 9,600 |
| Total | 20 | 19 | 22,800 |

Every proposed request uses Groq `openai/gpt-oss-20b`, capped at 1,200 output tokens.
Retries and failover are prohibited in the proposed run; stop on quota/provider
failure. The no-source case is an explicit local refusal. Actual source/request
hashes, exact messages and separate budgets are in the
[manifest](answer-comparison-preflight-v1.json). Prompt text totals 56,393 characters;
characters/4 gives a rough 14,099 input-token estimate, not billing or cost.

The new tool only prepares data and does not enforce a live quota. A future runner
must check these hashes and persist each attempt before calling a provider with
SDK retries disabled. The previous 11-request approval is exhausted; this pack
requires a new explicit budget approval before any execution.

Reproduce offline with a **new** output path:

```bash
.venv/bin/python scripts/prepare_answer_comparison.py --output /private/tmp/new-comparison-pack.json
```

Existing output paths are never overwritten. Known cases retain the old supplied
source order/pages/text; baseline answers and expected claims are never model
messages. Fresh cases are prepared from the authored PDFs. Passage/source limits
are checked without silent truncation. Original captures, historical manifests,
frozen question files and runtime code are unchanged.

This isolates synthesis with oracle sources. It does not measure retrieval,
production readiness or new graph extraction. The known baseline is one earlier
stochastic output, so model/provider variation limits causal attribution. Fresh
cases have no baseline outputs. All semantic scores and human reviews remain
unmeasured. Use the existing hash-bound review flow after approved captures.

Validation: five focused regression methods and 770 backend tests pass. One fresh
read-only review found a missing question-identity check; the correction was
verified RED→GREEN and now binds known question text to the original approved
manifest. Source/request hashes match. No second review. Frontend/runtime source
is unchanged. The fresh questions remain ungenerated and unscored.
