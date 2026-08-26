# 0002 — Reflection timing: depends on stakes

**Status:** Accepted — 2026-08-25
**Component:** Agent Runtime (10)

## Context

Reflexion's own pattern reflects once, after a full trajectory and its outcome are observed: trajectory → outcome → feedback → reflection → memory → next trajectory. But the literature also notes explicitly that a locally-correct step can still produce a globally incorrect trajectory — so waiting until the end to reflect means an error can propagate through several later steps before anything catches it.

Agent Runtime needed a rule for whether reflection ever happens mid-trajectory, and if so, when.

## Decision

Depends on stakes: step-level reflection only for high-stakes steps (the ones Policy & Safety would flag anyway); trajectory-level reflection, Reflexion's own pattern, always runs at the end regardless.

## Alternatives considered

- **Trajectory-level only.** Reflect once, after the outcome is observed — matches Reflexion's paper directly, cheapest. Rejected as the sole mechanism because errors compound before anything catches them, which is exactly the failure mode the literature flags as unaddressed by Reflexion alone.
- **Step-level always.** The runtime pauses and reconsiders after every step, not just high-stakes ones. Catches compounding errors earliest, but adds latency and cost to every single step, most of which don't need it. Rejected as disproportionate.

## Consequences

- Step-level reflection now depends on Policy & Safety's stakes signal being available *before* a step executes, not only after — this is a load-bearing assumption, see ADR-0003's related knowns/unknowns entry.
- Two reflection code paths exist instead of one (step-level and trajectory-level), which is more to build and keep consistent than either alternative alone.
- Trajectory-level reflection is never skipped, so the Reflexion-style memory feedback loop stays intact regardless of what happened at the step level.

## Related

- Full trajectory design: [Agent Runtime Design](https://claude.ai/code/artifact/89de3618-d0b8-44f3-af85-73dbfbd73df6), fig. 2 (the "stakes?" branch) and the knowns/unknowns grid (this exact validity question is listed as known-unknown).
- Logged narratively in `../checkpoint.md`.
