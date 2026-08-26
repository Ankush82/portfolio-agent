# 0006 — Memory structure: linked network (A-MEM)

**Status:** Accepted — 2026-08-26
**Component:** Memory (06)

## Context

A-MEM treats memories as interconnected structures rather than isolated records: a new memory is analyzed, a structured representation is created, related memories are found, links are created, and the memory network is updated. This is particularly relevant for historical financial events, where a single earnings miss connects to a company, a sector, and prior similar events. Memory needed a decision on whether this linking happens at write time or is left to be discovered later.

## Decision

Linked network: a new memory is analyzed against existing memories and gets explicit links at write time, rather than relatedness only being discovered when something searches.

## Alternatives considered

- **Independent records.** Memories are stored flat; relatedness is only discovered when something searches, never maintained as structure. Cheaper to write, but rejected because "how does this connect to what we already know" would then be recomputed from scratch on every retrieval instead of being remembered once.

## Consequences

- Every write now costs more: it triggers an analysis pass against existing memories before it's considered complete, not just a store operation.
- The link step happens *before* the poisoning-quarantine check (ADR-0007), which means a memory can be connected to trusted, existing memories before it's itself confirmed trustworthy — the failure framework names this explicitly: a malicious memory could attach to trusted memories and borrow their credibility through the link, and this is unresolved (unknown-known).
- Memory Consolidator's periodic pass (fig. 2 of the Memory design) has to re-trigger linking on update/invalidation, since the network can go stale the same way any single memory can.

## Related

- Full design: [Memory Design](https://claude.ai/code/artifact/e6718943-d0a4-40bc-8d1d-376d21504e28), fig. 1 ("Link to existing knowledge") and the knowns/unknowns grid.
- Logged narratively in `../checkpoint.md`.
