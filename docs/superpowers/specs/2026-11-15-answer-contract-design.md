# Answer completeness and citation discipline

User authorization: continue the agreed answer-quality roadmap, Step 4, locally.
Intent: prevent structurally unusable provider answers from being presented as
successful and retain accepted evidence when providers fail. No semantic score.

Approaches: reuse a deterministic citation contract (selected; offline, no added
requests), a new model judge (not authorized; adds cost and unverified semantics),
or require every accepted source to be cited (rejected; sources can be redundant).
The existing provider failover and evidence gate remain authoritative.

Runtime non-refusal answers need readable text and at least one canonical source
label. Every source index must exist and any explicit page must match its source.
Empty, echo-plus-citations, citation-only, malformed/unfinished and invalid-page
answers reuse InvalidAnswerError and existing provider failover/fallback paths.
The exact canonical refusal is allowed without a citation; its correctness still
needs semantic review. This is format/reference validation, not claim entailment.

Fallback quotes up to five accepted literal passages at the existing900-character
excerpt bound with original citation indices and visible limits. Source order,
API schema, provider selection, request accounting, cancellation and resource/store
lifecycle stay intact. Prompt requests a direct answer, cited details and explicit
missing details without assuming all sources contain independent required facts.
Offline audit reuses runtime parsing, leaving human correctness fields unscored.
New authored development/heldout contract fixtures freeze before implementation;
freeze reviewed rule before first heldout run, no tuning afterwards. No hosted
calls, production writes, credentials, new dependencies or deployment this turn.
