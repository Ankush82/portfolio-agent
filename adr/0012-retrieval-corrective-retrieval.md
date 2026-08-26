# 0012 — Corrective retrieval (CRAG)

**Status:** Accepted — 2026-08-26
**Component:** Retrieval & Context (05)

## Context

Corrective Retrieval Augmented Generation (CRAG) addresses the problem of poor retrieval directly: a Retrieval Evaluator judges what came back, and when it's bad, the system performs corrective retrieval — external search, reconstructed context — rather than proceeding on weak documents. CRAG's stated architectural insight is that "no useful evidence found" should be a legitimate system state. Retrieval & Context needed a decision on whether a bad retrieval gets corrected or simply passed through.

## Decision

Corrective retrieval (CRAG): a Retrieval Evaluator judges sufficiency; insufficient results trigger bounded corrective/external search rather than proceeding. Once the retry budget is exhausted, "no useful evidence found" becomes the explicit terminal state, not a silently empty context.

## Alternatives considered

- **Pass-through.** Whatever the retriever returns goes straight to context construction; Evidence & Verification catches problems downstream instead. Rejected because it pushes a retrieval-quality problem into the evidence layer, where it looks indistinguishable from an evidence problem — the two failure classes get conflated instead of being caught at their actual source.

## Consequences

- Requires a bounded retry budget for corrective retrieval, mirroring the same pattern Agent Runtime uses for replan (ADR-0004) — an explicit, deliberate reuse of that shape rather than a new one invented here.
- Corrective retrieval's external search introduces a new, less-vetted source into a context the rest of the system otherwise treats as equally trustworthy — named as an unknown-unknown in the failure framework, since nothing yet distinguishes a corrective-search source from an ordinary one downstream.
- "No useful evidence found" now has to be a state Analysis & Reasoning (not yet designed) can actually handle, not just a value it happens to receive.

## Related

- Full design: [Retrieval & Evidence Design](https://claude.ai/code/artifact/afae265c-32a2-460f-b537-24a4cfc736d4), fig. 1 (the "sufficient?" evaluator, Corrective Retrieval, and the "no useful evidence found" terminal state).
- Logged narratively in `../checkpoint.md`.
