# 0031 — Claim verification: citation completeness and confidence scoring

**Status:** Accepted — 2026-08-26
**Component:** Evidence & Verification (09)

## Context

`ClaimVerifier.verify()`'s docstring names "citation quality and completeness (ALCE)" as its job; `score_confidence()` is specified only as producing a number from a `VerifiedClaim`. Neither prior ADR (0013, 0014) nor the component's design artifact specifies the actual formula. Two things needed a real, computable answer:

1. What does "structural citation checking" concretely mean without an LLM judging claim-evidence entailment — ALCE's own citation-completeness idea (does a claim have enough independent support) is implementable structurally, but "enough" and "independent" both need real definitions.
2. How does `score_confidence()` turn `Evidence.reliability`/`freshness` and the claim's citation shape into one number, including the specific case ADR-0014 named but left unresolved: a claim whose evidence disagreed and was automatically resolved should be distinguishable, in the number, from a claim whose evidence agreed cleanly — ADR-0014's Consequences calls this "differing only in the number attached" and treats that as the acceptable outcome, not a gap to close some other way.

## Decision

**Citation completeness: independent-source count against a target.** "Independent" is operationalized as a distinct `Evidence.source` string (`memory:<scope>:<id>` per ADR-0029 for Memory-sourced evidence, a document's own `source_id`/`source` for ContextPack-sourced evidence — already distinguishable because they come from different origins by construction). A claim is fully cited once it has `_TARGET_INDEPENDENT_SOURCES = 2` independent sources; a `source_diversity_factor = min(independent_sources / 2, 1.0)` scales confidence down for a claim resting on a single source and caps out (no further bonus) once a second independent source is present.

**`verify()`**: composes a `ContradictionResolver` (constructor-injected) to determine `was_contradictory` for real — `not contradiction_resolver.sources_agree(evidence)` — and carries the full `evidence` list forward on `VerifiedClaim` unmodified, so a downstream reader can see exactly how much evidence backed a claim and from where, not just a final score. Empty evidence returns `VerifiedClaim(evidence=[], confidence=0.0, was_contradictory=False)` directly, without calling the resolver on nothing — a defensive real-behavior case, since `MandatoryEvidenceGate` (ADR-0013) is expected to block empty-evidence claims before they reach `verify()`, but `verify()` doesn't assume that ordering is enforced by every caller.

**`score_confidence()`**: `confidence = base_quality * source_diversity_factor * contradiction_penalty`, clamped to `[0.0, 1.0]`.
- `base_quality` = mean `reliability * freshness` across all evidence when sources agreed, or the `ContradictionResolver.resolve()` winner's own `reliability * freshness` when they didn't (reusing ADR-0030's resolution rather than recomputing anything).
- `contradiction_penalty` = `_CONTRADICTION_RESOLUTION_PENALTY = 0.85` when `was_contradictory`, else `1.0` — the concrete number that makes an automatically-resolved contradiction score measurably lower than an equally-reliable claim that never disagreed, directly answering ADR-0014's "differing only in the number attached."

## Alternatives considered

- **No contradiction penalty** (score purely on the resolved winner's own reliability/freshness, identically to the non-contradictory case). Rejected: this is exactly the "differing only in the number attached" outcome ADR-0014 flags as a real risk, not an intended design — a resolved contradiction and a clean single source would then be genuinely indistinguishable to Decision & Policy, which is the RAGTruth concern ADR-0013/0014 both exist to guard against.
- **Unbounded source-diversity bonus** (confidence keeps increasing with every additional independent source, no cap). Rejected: nothing in ALCE's own citation-completeness framing suggests more sources beyond a reasonable threshold should keep compounding confidence upward, and an unbounded bonus would let volume of (possibly redundant) sourcing substitute for actual evidence quality — capping at 2 independent sources treats "reasonably triangulated" as the ceiling, not "as many sources as possible."
- **Hardcoded confidence bands** (e.g. return 0.9 for "agreed," 0.5 for "contradictory," 0.1 for "single source") instead of a computed formula. Explicitly what the task instruction ruled out ("not hardcoded") and what this ADR avoids: the formula is sensitive to the real `reliability`/`freshness` values on the actual evidence, not a lookup table keyed on category.

## Consequences

- `score_confidence()`'s output is only as trustworthy as the `reliability`/`freshness` values feeding it, which themselves depend on ADR-0029's freshness half-life and whatever upstream component set `reliability` (Memory's `confidence`, or a ContextPack document's own field, or the `0.5` neutral default). A systematically miscalibrated upstream reliability signal would silently miscalibrate every confidence score downstream of it — a real known-unknown this ADR inherits rather than resolves.
- `_TARGET_INDEPENDENT_SOURCES = 2` and `_CONTRADICTION_RESOLUTION_PENALTY = 0.85` are named constants, not magic numbers, and both are constructor-overridable — but their specific values are this ADR's judgment call, not derived from any cited study of this project's own evidence distribution. If block-rate/confidence-distribution drift (Observability & Governance, ADR-0017) later shows these are miscalibrated for how this system's evidence actually behaves, that's a constant change here, not a reversal of this ADR's structure.
- Independent-source counting via `Evidence.source` string equality means two evidence entries that are substantively from the same underlying source but tagged with slightly different source strings (e.g. two different Memory ids that both happen to quote the same original filing) would count as "independent" when they arguably aren't — a real limitation of using a string identity proxy for true source independence.

## Related

- Depends on: [ADR-0013](0013-evidence-mandatory-per-claim.md) (why empty-evidence claims are expected to be blocked before reaching `verify()`), [ADR-0014](0014-evidence-automatic-contradiction-resolution.md) (the "differing only in the number attached" framing this ADR's contradiction penalty directly answers), [ADR-0029](0029-evidence-linking-relatedness-and-search-mechanism.md) (the `reliability`/`freshness` values this ADR weights), [ADR-0030](0030-evidence-contradiction-detection-and-resolution-weighting.md) (`sources_agree()`/`resolve()`, composed here rather than reimplemented).
- Implemented by: `../src/components/c09_evidence_verification.py`, `DefaultClaimVerifier`.
- Logged narratively in `../checkpoint.md`.
