# Citation compatibility and literal condition prominence

Implemented offline after the consumed 19-request comparison. No new model call,
semantic score or human review. The previous raw outputs and audits are unchanged.

Exact complete typographic labels such as `【Source 1】` and
`【Source 1, p. 3】` are normalized to standard square brackets before runtime
validation. Prose, source indices and page numbers are preserved. Malformed,
unfinished, mixed-bracket, absent-source and wrong-page references still fail.
The offline audit shares this interpretation. Canonical refusal remains allowed.

Source evidence containing typographic labels is rendered literally in fallback
excerpts using numeric entities after existing Markdown escaping. An actual
ReactMarkdown/GFM rendering check found extra backslashes in an initial approach;
four renderer assertions pass after correction. Escaped labels and previously
escaped HTML entities stay literal. No citation destination is invented.

The generation prompt also highlights up to five literal condition sentences
from accepted sources, with at most 900 total quote characters. The source number
and exact quote are supplied as evidence, while the full original excerpts and
source order remain intact. Selection uses limited English markers for scope,
negation and conditions, with question-term overlap for stable priority. It does
not infer missing constraints, prove entailment or veto semantic claims. Unterminated final fragments are excluded, including passages
already shortened by retrieval. This can omit a complete sentence lacking final
punctuation; full source excerpts remain available. The model is instructed to check dependencies
and guarantees, preserve subject/scope/negation and omit unsupported consequences.

| Offline verification | Result |
| --- | ---: |
| Baseline backend tests | 770 passed |
| Current backend tests | 781 passed |
| New focused methods | 11 passed |
| Frontend tests / TypeScript+Vite build | 9 passed / passed |
| Installed Markdown renderer assertions | 4 passed |
| Consumed capture format replay | 17/20 → 20/20 |
| Hosted-output subset format replay | 16/19 → 19/19 |
| New provider calls / human reviews | 0 / 0 |

The replay recovers three formatting failures. **It does not correct qa06's
unsupported semantic claims**, nor establish better generated answers. No
outputs were regenerated and the used fresh set remains consumed. New wrong-
reference/echo/escaped-text regression inputs are synthetic format checks.

The extra guidance has a prompt-size tradeoff on the old oracle contexts:
known eleven requests grow from 33,690 to 39,553 prompt characters; the eight
consumed fresh contexts grow from 22,703 to 26,598. All assembled text continues
to be counted by provider admission, including Gemini's separator. These are
character counts, not measured tokens, latency, billing or a cost improvement.

No API, auth, source eligibility/threshold/cap, retrieval ranking, graph projection,
worker, checkpoint, retry, deletion, resource lifecycle, environment or cross-store
consistency change is part of this increment. The existing provider fallback
handles invalid references; a valid normalized answer does not call a backup.
Broader Markdown/HTML contexts remain outside this mechanical parser's coverage.

Next: human review of claim support and missing detail, plus separately approved
new captures to test model adherence. Use another fresh set for future generalization
measurement. Both prior hosted allowances are exhausted. The older preflight's
runtime hashes are now historical and must not be reused for execution.

[Exact replay rows, source hashes and bounds](citation-scope-revision-current.json).

Completed locally on 16 November 2026. One fresh read-only review identified a
retrieval-clipped sentence being highlighted as complete. The `build_sources()`
regression reproduced the defect, then passed after conservatively excluding
unterminated fragments. All 781 backend tests passed after that correction. The
nine frontend tests, TS/Vite build and four renderer assertions passed before the
final Python-only fragment fix; frontend/rendering code did not change afterward.
One review and one correction pass; no second review or new model call.
