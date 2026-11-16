# Citation compatibility and source-condition prominence

Baseline: 17e9171, with the 19-request capture complete and both budgets exhausted.
Work is offline. Scope: canonicalize exact complete typographic source labels,
retain all index/page/passages/echo/refusal checks; share behavior with auditing;
quote source conditions in the prompt and request a dependency/guarantee check.
No general semantic verifier or new hosted requests. Preserve API, auth, evidence
thresholds, accepted source order/text, ingestion and cross-store lifecycle.

- [x] Nine synthetic regression methods RED before implementation.
- [x] Strict normalizer/parser, runtime normalization and fallback text protection.
- [x] Up to five source-indexed literal condition sentences, 900 total quote chars,
  stable question-term ordering, no inference or mutation. Full sources retained.
- [x] Additional clipped-sentence regression RED→GREEN; ten focused methods pass.
- [x] Full suite: 780 backend tests pass (2.578s before final rendering correction).
- [x] Verify actual ReactMarkdown/GFM rendering and frontend checks.
- [x] One fresh whole-increment review; one correction pass as needed.
- [x] Replay consumed captures for format compatibility only; preserve original
  outputs/audits and report exact before/after plus prompt-size tradeoff.
- [x] Record final tests, honest limits, and scoped local commit.

Ruling: source-condition selection is bounded lexical highlighting with stronger
prompt guidance, not semantic entailment or a new acceptance veto. Cost if wrong:
a model can still generalize or omit caveats; no accuracy gain may be claimed
without new approved generation and actual human review. The used fresh set is
consumed and is never reported as new heldout evidence.

Final: fixed P2 pre-truncated source fragment promotion — regression using actual
`build_sources()` RED→GREEN, focused 11/11 and full suite 781/781 (2.757s).
One final review, one correction pass, no second review. Rendering 4/4, frontend
9/9 and TS/Vite build passed before the final Python-only clipping correction.
Format replay still 17/20→20/20; runtime source/report hashes verified. Existing
raw capture, question and historical report files retained byte-for-byte.

Ruling: omit every unterminated final fragment from highlights because retrieval
provides no trustworthy completeness flag. Cost if wrong: a valid unpunctuated
condition will lack extra highlighting; its original excerpt is still supplied.

Local implementation and verification completed 16 November 2026. No new hosted
quota, runtime activation flags, production data, environment edits, push or deploy.
