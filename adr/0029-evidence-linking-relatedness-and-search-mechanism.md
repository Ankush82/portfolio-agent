# 0029 — Evidence linking: relatedness rule and Memory/ContextPack search mechanism

**Status:** Accepted — 2026-08-26
**Component:** Evidence & Verification (09)

## Context

`EvidenceLinker.link(claim: Claim) -> list[Evidence]` (c09_evidence_verification.py) is specified to search "Context Pack (component 05) and Memory (06)," but no prior ADR settled two implementation questions the design left open:

1. **Relatedness rule.** How does "search for content related to the claim" actually get computed, without an LLM or embedding model — no such provider has been chosen anywhere in this project (ADR-0021, ADR-0028).
2. **Search mechanism shape.** `link()`'s own signature takes only a `claim`, with no parameter carrying a `MemoryManager`, a `ContextPack`, or anything else to search *through*. Something has to decide how those get supplied to a `Default*` implementation without changing the `EvidenceLinker` Protocol itself.

Per the operating-mode instruction governing this build pass, this is a real implementation gap to resolve with documented engineering judgment, not a stop-and-ask.

## Decision

**Relatedness rule: Jaccard token overlap**, the same rule `DefaultEntityLinker` already uses in `src/components/c06_memory.py` for the identical reason (no embedding/LLM provider exists). A claim's `text` and a candidate's content (a `Memory.content` dict, or a `ContextPack` document dict) are each flattened to a lowercase token set; the claim is linked to the candidate when `|intersection| / |union|` meets `similarity_threshold` (constructor-configurable, default `0.2`).

**Search mechanism: constructor injection, not a Protocol change.** `DefaultEvidenceLinker` takes a real `MemoryManager` (c06) at construction and always searches it, across a configurable set of scopes (default `("user", "shared")` — both physically separate stores per ADR-0008). A `ContextPack` is different in kind: it changes per query, not once per linker instance, so `DefaultEvidenceLinker` supports it two ways — bound once at construction (`context_pack=`, for a caller with one fixed retrieval round) and per call via `link_with_context(claim, context_pack)`, a real extra method beyond the `EvidenceLinker` Protocol (the same "extra real-behavior accessor" pattern `DefaultQuarantineGate.release()`/`is_expired()` already establishes in c06_memory.py for exactly this situation — real behavior a fixed Protocol signature has no room for).

A quarantined `Memory` (ADR-0007) is never surfaced as evidence — quarantine means "not yet trusted," which is the opposite of what evidence is for.

**Evidence.reliability / Evidence.freshness, computed, not defaulted to zero:**
- From `Memory`: `reliability = memory.confidence` (the trust signal the dataclass already tracks); `freshness` decays from `memory.last_touched_at` with a 30-day half-life (`freshness = 0.5 ** (age_seconds / 30_days)`), a moderate choice for a financial system — recent enough that month-old evidence is meaningfully discounted, not so aggressive that a week-old filing reads as stale.
- From a `ContextPack` document (a free-form `dict` — neither c04 nor c05 specify a required shape): `reliability` and `freshness`/`timestamp` are read from the document if present, else default to `0.5` (neutral — no claim either way) and `1.0` (a document just came out of a retrieval that ran right now) respectively.

## Alternatives considered

- **Embedding/semantic similarity search.** The technically better relatedness signal, and the more literal reading of "search for content related to the claim" — but requires an embedding model, and this project has never chosen an LLM/embedding provider for any component (ADR-0021, ADR-0028 are both still Proposed for exactly this reason). Rejected for now on the same grounds those two ADRs use: pick this later behind the same interface once a provider decision is made, rather than deciding it here by fiat.
- **Extending the `EvidenceLinker` Protocol's `link()` signature** to accept `memory_manager`/`context_pack` parameters directly. Would make the search inputs explicit at the call site, but breaks the Protocol every other component's design already references (fig. 2 draws `link(claim)` as the interface), and `StubEvidenceLinker` would need to change too. Rejected in favor of constructor injection plus a non-Protocol extra method, consistent with how `DefaultQuarantineGate` handles the same "real Protocol is too narrow for real behavior" problem in c06_memory.py.
- **Reliability/freshness defaults of 0.0 for undocumented `ContextPack` fields.** Simpler, but throws away real signal for no reason — a document that was just retrieved genuinely is fresh, and scoring it 0.0 would make every ContextPack-sourced claim look untrustworthy regardless of content. Rejected; see `_CONTEXT_PACK_DEFAULT_RELIABILITY`/`_CONTEXT_PACK_DEFAULT_FRESHNESS` in the implementation for the neutral defaults actually used.

## Consequences

- This inherits the same honesty caveat `DefaultEntityLinker` already carries: token overlap is a real, deterministic, structural relatedness signal, not semantic understanding. Two claims about the same fact phrased with no shared vocabulary will not link; two claims sharing common words but about different subjects might. Named here as the same known-unknown ADR-0006/ADR-0010 already carry for Memory's own linking.
- `link()` alone (no bound `context_pack`) only ever searches Memory — a caller that wants ContextPack coverage on every call must either bind one at construction or call `link_with_context()` explicitly. This is a real, stated scope limit, not a silent gap: `link()`'s own Protocol signature has no room to carry a fresh `ContextPack` per call.
- Both freshness formulas (Memory's half-life, ContextPack's default-to-fresh) are real, computable, and now load-bearing on every downstream confidence score (ADR-0031) — if they turn out miscalibrated for how quickly financial evidence actually goes stale, that recalibration is a constant change in this file, not an architecture change.

## Related

- Depends on: [ADR-0006](0006-memory-linked-network-structure.md) (`DefaultEntityLinker`'s Jaccard-overlap precedent), [ADR-0007](0007-memory-quarantine-at-write.md) (why quarantined memories are excluded), [ADR-0008](0008-memory-structural-partition.md) (why both scopes are searched separately), [ADR-0013](0013-evidence-mandatory-per-claim.md) (what this evidence feeds into).
- Extends the same "ask, don't decide" pattern deferred elsewhere: [ADR-0021](0021-agent-runtime-llm-provider-interim.md), [ADR-0028](0028-memory-mem0-llm-embedding-provider-interim.md) (embedding/LLM provider, not decided here either).
- Implemented by: `../src/components/c09_evidence_verification.py`, `DefaultEvidenceLinker`.
- Logged narratively in `../checkpoint.md`.
