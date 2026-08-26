# 0014 — Contradictory evidence: resolved automatically

**Status:** Accepted — 2026-08-26
**Component:** Evidence & Verification (09)

## Context

The literature's own design questions ask explicitly how contradictory evidence should be represented — a real case for a financial system, where one source might say an earnings report beat expectations and another says it missed. Evidence & Verification needed a decision on what happens once two pieces of linked evidence disagree.

## Decision

Resolve automatically: weight by source reliability and freshness, pick the higher-confidence side, pass one answer downstream.

## Alternatives considered

- **Surface as its own state.** "Contradictory evidence" becomes a distinct output Decision & Policy can see and act on differently — lower actionability, request more evidence — rather than being silently collapsed into one answer. Not chosen: rejected in favor of resolving automatically so that Decision & Policy (not yet designed) receives one answer with a confidence score, consistent with how every other claim arrives, rather than needing a second code path for the contradictory case.

## Consequences

- Depends entirely on source reliability and freshness being themselves reliably known and comparable across very different source types — a company filing versus a news article versus market data. That scoring hasn't been designed yet; this ADR assumes an answer to it. Named as unknown-known in the failure framework.
- RAGTruth's own finding, that RAG doesn't automatically eliminate unsupported claims, applies with extra force here: an automatically-resolved contradiction is exactly the shape of a claim that can look well-supported (it has a confidence score, it has evidence) while actually being wrong. Named as known-unknown.
- No mechanism currently distinguishes "this claim had one clean source" from "this claim won a disagreement between two sources" once it reaches Decision & Policy — both arrive looking the same, differing only in the number attached.
- If Decision & Policy (not yet designed) later needs to treat resolved-contradiction claims differently, that's a design change there, not a reversal of this ADR — the automatic-resolution decision only concerns Evidence & Verification's own output shape.

## Related

- Full design: [Retrieval & Evidence Design](https://claude.ai/code/artifact/afae265c-32a2-460f-b537-24a4cfc736d4), fig. 2 (the "sources agree?" branch and "Resolve automatically").
- Logged narratively in `../checkpoint.md`.
