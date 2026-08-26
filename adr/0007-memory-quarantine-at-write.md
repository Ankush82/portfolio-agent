# 0007 — Memory poisoning defense: quarantine at write

**Status:** Accepted — 2026-08-26
**Component:** Memory (06)

## Context

Memory poisoning is its own named failure class in the literature: untrusted information gets written into persistent agent memory and later influences agent behavior, compounding — bad analysis → memory → future analysis → more bad analysis → more memory. Because Memory is meant to close the system's learning loop (prediction → outcome → evaluation → memory → future prediction), a poisoned memory doesn't just cause one bad answer, it corrupts future reasoning. Memory needed a decision on where the defense sits.

## Decision

Quarantine at write: a memory derived from untrusted or unverified content is flagged and held back before it becomes usable memory. The gate is at Store.

## Alternatives considered

- **Trust-weighted at read.** Everything gets stored, but every memory carries provenance and confidence; low-trust memories are always down-weighted or require corroboration when retrieved — the gate is at Retrieve, not Store. Rejected as the primary defense because it lets unverified content sit in the store as if it were ordinary memory from the moment it's written, relying entirely on every future read path to apply the weighting correctly rather than stopping the write.

## Consequences

- Quarantined memories need their own lifecycle: fig. 1 of the Memory design specifies they're released once corroborated and expire if never corroborated, so they don't sit forever. That lifecycle policy didn't need to exist under the read-time alternative.
- The failure framework is explicit that this is not a solved problem: whether quarantine-at-write actually stops a sophisticated, deliberately-crafted poisoning attempt, or only catches the obvious cases, is listed as known-unknown, mirroring the memory-poisoning paper's own framing.
- Corroboration itself is assumed to be achievable for anything held in quarantine — some claims may never be independently verifiable, and what happens then, beyond "expires," is not yet designed (unknown-known).

## Related

- Full design: [Memory Design](https://claude.ai/code/artifact/e6718943-d0a4-40bc-8d1d-376d21504e28), fig. 1 (the "provenance verified?" gate and the Quarantine box) and the knowns/unknowns grid.
- Logged narratively in `../checkpoint.md`.
