# 0005 — Memory management: active (MemGPT-style)

**Status:** Accepted — 2026-08-26
**Component:** Memory (06)

## Context

MemGPT's architectural insight is that memory is an active resource-management mechanism, not simply a vector database: instead of putting everything into the context window, the system actively manages what enters and leaves a bounded working context, moving the rest to external memory. Memory needed a decision on whether it plays this active role or stays a passive responder.

## Decision

Active management: Memory Manager curates a bounded working set on its own logic, evicting to archive rather than only ever responding to Store/Retrieve calls from other components.

## Alternatives considered

- **Passive store.** Memory only ever does what it's told — store this, retrieve that — with Agent Runtime or Analysis deciding what's relevant. Simpler to build, but rejected because nothing in the system would then own the question of "what should I be holding onto right now," and that question was exactly MemGPT's point of departure from treating memory as a plain database.

## Consequences

- Memory Manager now needs an eviction policy (what leaves the working set when it's full) — designed in fig. 2 of the Memory design as weighing recency and relevance, not just age, specifically to avoid thrashing.
- Adds real complexity Memory wouldn't otherwise have: a working set is now stateful and actively curated, not just a cache in front of storage.
- The long-run effect of the eviction policy on what the system quietly stops remembering is named explicitly as an unknown-unknown in the failure framework — this is a cost of choosing "active" that a passive store wouldn't have incurred.

## Related

- Full design: [Memory Design](https://claude.ai/code/artifact/e6718943-d0a4-40bc-8d1d-376d21504e28), fig. 2 (Memory Manager, working set, eviction).
- Logged narratively in `../checkpoint.md`.
