# Actual current-prompt answer comparison

Completed 15 November 2026 under the new explicit 19-request approval. All 19
requests completed normally (`finish_reason=stop`); no retry, fallback provider,
embedding, reranking or production test-data mutation occurred. Model alias:
`openai/gpt-oss-20b`. One additional no-source refusal was recorded locally.

| Measure | Historical known baseline | Current known questions | Fresh candidate only |
| --- | ---: | ---: | ---: |
| Hosted requests | 11 | 11 | 8 |
| Responses including local refusal | 12 | 12 | 8 |
| Mechanical answer contracts valid | 12/12 | 11/12 | 6/8 |
| Recognized citation labels | 17 | 15 | 8 |
| Recognized labels with wrong index/page | 0 | 0 | 0 |
| Literal supplied passages | 21/21 | 21/21 | 10/10 |
| Known missing-fact model refusals | 2/2 | 2/2 | — |
| Fresh expected refusal returned | — | — | 1/1 |
| Reported input tokens | 5,201 | 7,247 | 4,909 |
| Reported completion tokens | 2,138 | 2,228 | 2,292 |
| Reported total tokens | 7,339 | 9,475 | 7,201 |
| Known hosted-answer text characters | 6,379 | 3,879 | — |
| Human semantic reviews | 0 | 0 | 0 |

**The result is mixed.** The eleven known answers contain 2,500 fewer characters,
but their reported token usage grew. Concision does not establish correctness or
lower cost. Three raw candidate answers (`qa06`, `sfh07`, `sfh08`) use `【Source …】`
and fail the existing contract. The parser's zero wrong-index/page count covers
recognized labels only; it does not mean all answers had usable citations. There
were 16/19 valid hosted outputs, or 17/20 including the separately local refusal.
In normal routing these invalid outputs can trigger the existing failover path;
this capture deliberately did not retry or invoke that path.

## Assistant observations, not human-reviewed scores

- `qa01` removes the unsupported HTTP method/header/status examples present in
  the historical response and gives the stated fields directly (555→148 chars).
- `qa04` removes the repeated multi-section explanation (1,122→458 chars).
- `qa06` still strengthens a learning prerequisite into transactional guarantees
  and adds an unsupported state-corruption consequence. Its citation format is
  also invalid. Prompt instructions alone did not resolve this known failure.
- `qa07` now explicitly mentions idempotency along with external-store rollback
  limits and exact-upload cleanup. Its oracle sources still do not fix q13's
  separate retrieval miss.
- `qa08` still folds quota checks into readiness and omits the explicit caveat
  that a healthy process does not establish provider quota. It also characterizes
  readiness failure and 429 more broadly than the supplied evidence supports.
- Fresh `sfh04` preserves the course/workflow qualifier and explicitly avoids
  claiming every idempotent implementation needs a SQL transaction.
- Fresh `sfh05` retains Retry-After and says a later attempt is not guaranteed to
  succeed. `sfh06` refuses the unsupported TLS detail.
- Fresh `sfh07` states header names are missing; `sfh08` returns the cited pressure
  and inspection values, but both fail citation-format validation.

These are qualitative assistant readings for subsequent human review. They are
not fabricated human labels, a hallucination percentage, or broad accuracy gains.

## Budget, provenance and limits

The complete new run used **12,156 input + 4,520 completion = 16,676 tokens**.
Largest completion: 451, below the approved 1,200-per-request cap. SDK latency
p50 0.652s; nearest-rank p95 1.209s. These exclude 20-second pacing, retrieval and
application overhead; they are not production or capacity measurements.

The historical capture set temperature 0; the approved current manifest omitted
that field and uses the provider default. Requested model aliases match, but an
identical backend model revision is not guaranteed. One stochastic output per
question and sampling differences prevent causal attribution to the prompt.
Known questions are diagnostic; fresh cases have no paired baseline and their
checklists are assistant-authored. The fresh set is now consumed. Do not tune on
it and report it as unseen again. All source/request hashes matched before calls.

The existing admission hook was used, with shared DB budgets disabled in local
configuration and no settings changed. The isolated runner enforces count/caps,
SDK `max_retries=0`, durable no-resend attempts and stop on provider/quota failure.
It refuses to run again when its capture ledger exists. The 19-request allowance
is exhausted. No more hosted calls are authorized by that approval.

The structural auditor also reports an old saved graph snapshot; those counts
are not new graph measurements. Its `provider_calls=0` refers to the offline audit,
not this separately approved preceding capture. Human semantic metrics stay null.

## Artifacts and next work

- [Exact run usage, attempt metadata and paired lengths](answer-comparison-assessment-current.json)
- [Known raw responses](answer-comparison-known-current.responses.json) and [structural audit](answer-comparison-known-current.json)
- [Fresh raw responses](answer-comparison-fresh-current.responses.json) and [structural audit](answer-comparison-fresh-current.json)
- Unfilled hash-bound worksheets: [known](answer-comparison-known-current.review.json), [fresh](answer-comparison-fresh-current.review.json)
- [Immutable approved preflight](answer-comparison-preflight-v1.json)

Next: address citation-format compatibility with offline regressions, review
claim support/qualifiers and missing details, and preserve strict refusals. Keep
retrieval evidence work separate. Use new questions before any subsequent heldout
measurement. No runtime/prompt change, deployment or new quality percentage is
part of this capture.
