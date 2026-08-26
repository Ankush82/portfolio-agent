# 0011 — Adaptive retrieval (Self-RAG)

**Status:** Accepted — 2026-08-26
**Component:** Retrieval & Context (05)

## Context

Standard RAG treats retrieval as unconditional preprocessing: Query → Retriever → Documents → LLM → Answer. Its weakness is that retrieval errors propagate directly into generation. Self-RAG's architectural insight is that retrieval should be a decision-making component rather than an unconditional preprocessing step — the system should first ask whether retrieval is even needed for this query. Retrieval & Context needed a decision on whether to adopt that gate.

## Decision

Adaptive (Self-RAG): Retrieval & Context includes a "should I retrieve?" gate; for some queries the answer is no, and Analysis proceeds on existing context or memory alone.

## Alternatives considered

- **Unconditional.** Retrieval always runs when asked, no self-gating — simpler, matches standard RAG. Rejected because it risks fetching irrelevant or redundant context even when nothing new was actually needed, which is exactly the weakness Self-RAG identified in the standard pattern.

## Consequences

- Retrieval & Context now carries a judgment call it didn't have to make before — whether retrieval is needed — and that judgment can be wrong in either direction (skipping when fresh information was actually required, or retrieving when it wasn't). This is named explicitly as known-unknown in the failure framework: Self-RAG's own paper treats it as a judgment call, not a solved rule.
- Saves retrieval calls and avoids diluting context with irrelevant documents when the gate correctly says no.
- Whatever decides the gate (a model call, a heuristic, a confidence threshold) is not yet designed — this ADR settles the shape, not the mechanism.

## Related

- Full design: [Retrieval & Evidence Design](https://claude.ai/code/artifact/afae265c-32a2-460f-b537-24a4cfc736d4), fig. 1 (the "should I retrieve?" gate) and the knowns/unknowns grid.
- Logged narratively in `../checkpoint.md`.
