# 0010 — Memory technology: Mem0

**Status:** Accepted — 2026-08-26 (vendor picked); vendor fit against the four decisions below not yet formally verified — see Consequences.
**Component:** Memory (06)

## Context

With Memory's design complete and confirmed — active working-set management (ADR-0005), linked network structure (ADR-0006), quarantine-at-write (ADR-0007), structural partition (ADR-0008) — a concrete storage/technology approach needed to be chosen for it, against that design rather than in the abstract.

## Decision

A purpose-built memory layer — **Mem0** — over building the storage ourselves and over Supermemory. Open-source and self-hostable, chosen directly by the user; not selected via the formal per-decision evaluation this ADR originally called for (see Consequences — that check is now the responsibility of whoever implements component 06 for real).

## Alternatives considered

- **Unified store (PostgreSQL + pgvector + adjacency tables).** One system implements all four decisions directly: separate schemas for the partition, a status column for quarantine, adjacency tables for links, pgvector for retrieval. Rejected as the default because it means building and maintaining all four mechanisms by hand, when tools already exist that claim to solve pieces of this.
- **Specialized per concern (knowledge graph for links + vector DB for retrieval + Postgres for scope/quarantine bookkeeping).** Each piece is the best tool for that one job. Rejected because keeping three systems consistent with each other is its own ongoing cost, for a component whose write path already has four sequential gates to keep consistent internally.

## Consequences

- **The evaluation this ADR originally required still has to happen — it's deferred, not skipped.** Before component 06's `Default*` adapters are written, Mem0 needs to be checked against: does it support structural partition (ADR-0008) as separate stores or only as metadata; does it support quarantine-at-write (ADR-0007) or only post-hoc filtering; does it support A-MEM-style explicit linking (ADR-0006) or only similarity search; does it support active working-set curation (ADR-0005) or only passive storage. Per `loop.md` step 2: if Mem0 doesn't fit one of these cleanly, that's a gap to flag with a draft ADR, not something to quietly work around in the implementation.
- If Mem0 fails enough of these checks, the fallback alternatives listed above (unified store, or specialized-per-concern) are still live options, not foreclosed by this decision.

## Related

- Full design: [Memory Design](https://claude.ai/code/artifact/e6718943-d0a4-40bc-8d1d-376d21504e28).
- Build sequence: [Implementation Plan](https://claude.ai/code/artifact/2b28cf66-452e-4ed8-b7a1-8f11580325fa), Phase 5.
- Logged narratively in `../checkpoint.md`.
