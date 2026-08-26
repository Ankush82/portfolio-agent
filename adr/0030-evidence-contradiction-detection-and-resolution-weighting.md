# 0030 — Contradiction detection rule and resolution weighting formula

**Status:** Accepted — 2026-08-26
**Component:** Evidence & Verification (09)

## Context

ADR-0014 decided contradictory evidence gets resolved automatically, "weight by source reliability and freshness, pick the higher-confidence side" — but named its own gap: "depends entirely on source reliability and freshness being themselves reliably known and comparable... that scoring hasn't been designed yet." Two implementation questions remained genuinely open for `ContradictionResolver` (c09_evidence_verification.py):

1. **`sources_agree()`**: what, concretely, counts as two pieces of linked `Evidence` disagreeing? "One source says earnings beat, another says missed" (ADR-0014's own example) is intuitive, but turning that into a real comparison over `Evidence.content: dict` needs a rule.
2. **`resolve()`**: given `Evidence.reliability` and `Evidence.freshness` (both already real fields), what is the actual weighting formula that combines them into "pick the higher-confidence side"?

## Decision

**`sources_agree()`: topic-gated field comparison.** Fewer than two `Evidence` entries trivially agree. Otherwise, every pair is first checked for topical relatedness (Jaccard token overlap over their `content` dicts, threshold `0.15` — deliberately lower than `EvidenceLinker`'s own `0.2`, since two entries already linked to the same claim only need to clear a lower bar to be "about the same sub-topic" of that claim). For pairs that clear this bar, disagreement is a direct field conflict: any dictionary key present in both `content` payloads whose normalized (lowercased, stripped) values differ — e.g. `{"metric": "EPS", "result": "beat"}` vs. `{"metric": "EPS", "result": "missed"}` conflict on `"result"`. A pair with no shared keys is not directly comparable this way and is treated as not conflicting, not silently assumed to agree in some stronger sense — just outside what this rule can determine.

**`resolve()`: composite-weighted max.** Each `Evidence` is scored `reliability * freshness`; `resolve()` returns the entry with the highest composite. Ties break toward the higher `reliability` alone (a source is more inherently trustworthy independent of how recently it was touched, so reliability is the tiebreaker of first resort), then toward whichever entry appeared first in the input list.

## Alternatives considered

- **Weighted average of `reliability` and `freshness` as separate terms** (e.g. `0.7 * reliability + 0.3 * freshness`) rather than a product. Rejected: a product means a source that's maximally reliable but completely stale (freshness near 0) still scores near 0, which is the more defensible behavior for a financial system — a highly reliable but outdated earnings figure should not "win" a disagreement against a merely-decent but current one. A weighted sum would let a stale-but-reliable source dominate purely on the reliability term.
- **Full pairwise similarity (all shared and unshared keys, not just shared-key conflicts) for `sources_agree()`.** Considered scoring disagreement by overall dict similarity rather than isolating shared keys. Rejected: two evidence entries about the same earnings report legitimately carry different keys (one might report `"EPS"`, another `"revenue"`) without disagreeing at all — conflating "different fields reported" with "same field, different values" would produce false positives on exactly the kind of complementary (not contradictory) evidence this system should be able to combine.
- **Treating no-shared-keys pairs as agreement** rather than "not comparable." Rejected as overclaiming: this rule genuinely cannot determine agreement or disagreement when there's no directly comparable field, and the honest response is to not flag a conflict rather than assert one is confirmed absent. This mirrors ADR-0026's own precedent for `PlaceholderSourceFetcher` reliability scoring — an honestly-limited real signal, not a claim of certainty the mechanism can't back up.

## Consequences

- `sources_agree()`'s disagreement detection is exactly as good as the field overlap between two sources' `content` dicts. Two evidence entries phrased with no shared keys (e.g. one structured `{"eps_result": "beat"}`, another `{"result": "missed"}`) will not be flagged as contradictory even if a human reader would immediately see the conflict — a real known-unknown this ADR inherits from ADR-0014's own "reliability and freshness... comparable across very different source types" concern, now made concrete: comparability also depends on matching field names, which nothing in this design normalizes across source types yet.
- `resolve()`'s composite formula is now load-bearing on `DefaultClaimVerifier.score_confidence()` (ADR-0031) whenever a claim's evidence disagreed — a miscalibrated half-life or reliability scale anywhere upstream (ADR-0029, ADR-0026) propagates directly into which side of a contradiction "wins."
- Consistent with ADR-0014's own Related note: this ADR only concerns Evidence & Verification's internal resolution mechanism. If Decision & Policy (not yet designed) later needs to see *that* a claim's evidence disagreed, not just its resolved confidence, `VerifiedClaim.was_contradictory` (already on the dataclass) is exactly that signal — already threaded through by `DefaultClaimVerifier` (ADR-0031), not a gap this ADR needs to reopen.

## Related

- Depends on: [ADR-0014](0014-evidence-automatic-contradiction-resolution.md) (the decision this ADR makes concrete), [ADR-0029](0029-evidence-linking-relatedness-and-search-mechanism.md) (the token-overlap rule this ADR reuses for topic-gating, and the freshness/reliability values this ADR weights).
- Feeds into: [ADR-0031](0031-claim-verification-citation-completeness-and-confidence-scoring.md) (`DefaultClaimVerifier` calls `resolve()` when evidence disagreed).
- Implemented by: `../src/components/c09_evidence_verification.py`, `DefaultContradictionResolver`.
- Logged narratively in `../checkpoint.md`.
