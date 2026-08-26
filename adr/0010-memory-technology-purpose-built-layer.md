# 0010 — Memory technology: purpose-built memory layer (Mem0 or Supermemory)

**Status:** Accepted, partially — 2026-08-26
**Component:** Memory (06)

## Context

With Memory's design complete and confirmed — active working-set management (ADR-0005), linked network structure (ADR-0006), quarantine-at-write (ADR-0007), structural partition (ADR-0008) — a concrete storage/technology approach needed to be chosen for it, against that design rather than in the abstract.

## Decision

A purpose-built memory layer — Mem0 or Supermemory — over building the storage ourselves.

**Not yet decided:** which of the two. Neither has been checked against this design's four specific decisions. This is the one technology decision in this folder that is intentionally incomplete.

## Alternatives considered

- **Unified store (PostgreSQL + pgvector + adjacency tables).** One system implements all four decisions directly: separate schemas for the partition, a status column for quarantine, adjacency tables for links, pgvector for retrieval. Rejected as the default because it means building and maintaining all four mechanisms by hand, when tools already exist that claim to solve pieces of this.
- **Specialized per concern (knowledge graph for links + vector DB for retrieval + Postgres for scope/quarantine bookkeeping).** Each piece is the best tool for that one job. Rejected because keeping three systems consistent with each other is its own ongoing cost, for a component whose write path already has four sequential gates to keep consistent internally.

## Consequences

- Before Phase 5 of the build sequence starts, Mem0 and Supermemory must each be evaluated specifically against: does it support structural partition (ADR-0008) as separate stores or only as metadata; does it support quarantine-at-write (ADR-0007) or only post-hoc filtering; does it support A-MEM-style explicit linking (ADR-0006) or only similarity search; does it support active working-set curation (ADR-0005) or only passive storage. Whichever product fails more of these tests forces either a design compromise or a fallback to one of the rejected alternatives above.
- This ADR should be superseded once that evaluation happens and one product is actually chosen — it is deliberately left open rather than guessed at.

## Related

- Full design: [Memory Design](https://claude.ai/code/artifact/e6718943-d0a4-40bc-8d1d-376d21504e28).
- Build sequence: [Implementation Plan](https://claude.ai/code/artifact/2b28cf66-452e-4ed8-b7a1-8f11580325fa), Phase 5.
- Logged narratively in `../checkpoint.md`.
