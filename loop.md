# Harnessed Loop — Component Implementation

The code-phase counterpart to `design-framework.md`. That file governs how a component gets *designed*; this one governs how a subagent turns an already-designed component into code. One subagent, one component, one pass through this loop.

**This file does not decide architecture.** Every architectural decision a subagent needs already exists in `adr/`. This loop describes how to read and obey those decisions, and — critically — what to do when something isn't covered. A coding subagent runs unsupervised more often than this conversation has, which makes the "don't decide, flag it" discipline more important here, not less.

## Assumptions this loop makes — flag if any are wrong before subagents start

- **Implementation language: Python.** Inferred from the technology already chosen — LangGraph (Agent Runtime, ADR-0009) and Mem0 (Memory, ADR-0010) are both Python-native. Never explicitly decided; correct this first if it's wrong, since everything below assumes it.
- **Repo layout: monorepo**, one directory per component (`components/<number>-<slug>/`), consistent with System Infrastructure's interface-boundary decision (ADR-0019) — components stay swappable behind an interface, not by being in separate repos.
- **Test framework: pytest**, following from the language assumption above.
- **Subagents are spawned one per component**, each given this file plus the component's specific inputs (below) as its brief. A subagent does not choose which component it works on; that's still sequenced by the build order in the Implementation Plan.

## Inputs every subagent MUST read before writing any code

For the assigned component:

1. Every ADR in `adr/` tagged with this component. These are binding, not suggestions.
2. The component's design artifact — the fig. 1 / fig. 2 mechanism diagrams and its knowns/unknowns grid.
3. `checkpoint.md` — the reasoning behind each decision, not just the decision itself.
4. `design-framework.md` and the Implementation Plan — for this component's position in the build sequence and any chosen technology.
5. **The Phase 0 Cross-Cutting Design** (components 15–18). These apply to every component's code, not only their own directory. A subagent building any component must wire in:
   - Reliability & Resilience's failure classification and per-tool circuit breaker (ADR-0015, ADR-0016)
   - Observability & Governance's tracing, logging, and cost tracking (ADR-0017) — every call gets a span
   - Security & Privacy's boundary gate on every external-facing or cross-component call (ADR-0018's pattern)
   - System Infrastructure's interface boundary (ADR-0019) — never talk to Postgres, Redis, or object storage directly; only through the shared infrastructure interface

## The loop

```
FOR the assigned component:

  1. READ
     All inputs above, in full. Do not start writing code before this
     step is complete.

  2. CHECK FOR GAPS
     Does implementing this component require resolving something no
     ADR or design artifact already settled — a technology pick, a
     tradeoff, an edge case the design didn't address?

     IF yes:
       → STOP. Do not decide. Write a draft ADR (status: Proposed, not
         Accepted) in adr/ stating the gap, the real options, and a
         recommendation if one is obvious. Append it to checkpoint.md's
         open questions. Report it and wait for it to be resolved.
         A subagent doesn't get to skip the "ask, don't decide" pattern
         just because it's writing code instead of a diagram.
     IF no:
       → proceed to step 3.

  3. IMPLEMENT
     Build the component to match its design diagram exactly — the
     mechanism, not a simplified version of it. Every named decision
     box, branch, and gate in fig. 1 / fig. 2 becomes real control flow,
     not a comment saying it isn't implemented yet. Wire in the four
     cross-cutting concerns above as you go, not as an afterthought
     bolted on at the end.

  4. TEST THE FAILURE MODES, NOT JUST THE HAPPY PATH
     For every entry in the component's knowns/unknowns grid:

       known-known      a real test that triggers it and asserts the
                         designed response actually happens.

       known-unknown     an instrumentation hook (a span, a metric, a
                         log line) that lets Observability & Governance
                         actually see it happening in production — an
                         unvalidated risk can't be fixed, but it can be
                         made visible.

       unknown-known     the assumption gets asserted or documented in
                         code exactly where it's load-bearing, so a
                         future reader finds it before it silently
                         breaks something.

       unknown-unknown   nothing to test directly; confirm instead that
                         the specific drift signals this component's
                         "detect" note promised are actually being
                         emitted to Observability & Governance.

  5. DEFINITION OF DONE
     - Every ADR tagged to this component is satisfied — not partially,
       not "close enough."
     - Every known-known failure mode has a passing test.
     - Every cross-cutting concern (15–18) is wired in, not stubbed.
     - No new architectural decision was made silently — re-run step 2
       before calling this done, not only at the start.

  6. REPORT
     Append an entry to checkpoint.md: what was built, which ADRs it
     satisfies, any draft ADRs raised in step 2, and any deviation from
     the design diagram with a stated reason — same shape as an ADR's
     "Alternatives considered": if you deviated, say from what and why.
     Do not start the next component until this entry exists.

END FOR
```

## What a subagent must never do

- Never pick between two undecided options to keep moving — e.g. Mem0 vs. Supermemory (ADR-0010). That's a draft ADR, not a coin flip.
- Never skip a cross-cutting concern because "that's a different component's job." Components 15 through 18 apply everywhere, by design.
- Never simplify a diagrammed branch away because it's harder to implement — dropping the quarantine gate, or the corrective-retrieval loop, because the happy path was easier to ship. If it's in fig. 1 or fig. 2, it isn't optional.
- Never treat a "still open" flag in an existing ADR as resolved by whatever the code happens to do. Resolve it explicitly through step 2, or leave it visibly unresolved with a draft ADR — never silently.

## Still-open items this loop inherits

Not this file's job to resolve; listed so a subagent doesn't accidentally resolve them by default:

- Memory: Mem0 vs. Supermemory (ADR-0010).
- Security & Privacy: authority-check granularity, per task vs. per tool call — surfaced during Phase 0 research, never actually asked.
- Learning & Evaluation: whether it's in scope for this build pass at all, or deferred to a later round.
