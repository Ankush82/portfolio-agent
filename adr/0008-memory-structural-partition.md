# 0008 — Memory scope: structural partition

**Status:** Accepted — 2026-08-26
**Component:** Memory (06)

## Context

The literature's design questions explicitly ask what is user-specific versus globally shared. This system's memory holds both kinds: a company's earnings history or a sector's typical reaction pattern is knowledge any user's analysis can draw on; this user's preferences or portfolio history should not leak into another user's reasoning. Memory needed a decision on how that boundary is enforced.

## Decision

Structural partition: user-specific memory and globally shared memory live in physically separate stores (User Memory Store, Shared Memory Store). A query has to specify which it's asking.

## Alternatives considered

- **Single store, scope as metadata.** One memory store, every record tagged user-specific or shared; queries filter by the tag rather than by which store they hit. Rejected in favor of the structural version because a metadata tag can be set wrong, dropped in a bug, or bypassed by a query that forgets to filter — a physically separate store removes an entire class of that mistake by construction.

## Consequences

- The write path (fig. 1 of the Memory design) needs an explicit scope-routing step after provenance tracking, sending each memory to one store or the other — this is an extra decision point that the metadata alternative wouldn't have required.
- The read path (fig. 2) needs to know, per query, which store or stores to check — callers now have to be scope-aware, not just topic-aware.
- Whether a memory's scope is ever allowed to change after it's written, or whether a user's reaction should sometimes be usable as shared evidence about a market pattern, is flagged as an open, unresolved question (known-unknown and unknown-known respectively) — the structural partition makes that crossing harder by design, which is the point, but also means it isn't a decision this ADR has made either way.

## Related

- Full design: [Memory Design](https://claude.ai/code/artifact/e6718943-d0a4-40bc-8d1d-376d21504e28), fig. 1 (the "scope?" branch and the two stores) and the knowns/unknowns grid.
- Logged narratively in `../checkpoint.md`.
