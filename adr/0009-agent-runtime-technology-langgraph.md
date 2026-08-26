# 0009 — Agent Runtime technology: LangGraph

**Status:** Accepted — 2026-08-26
**Component:** Agent Runtime (10)

## Context

Technology selection was deliberately deferred until a component's low-level design was done (`portfolio_ai_three_literature_reviews.md`'s closing note explicitly names LangGraph and Temporal among the tools not to assume yet). With Agent Runtime's design complete and confirmed — hybrid checkpoint loop (ADR-0001), stakes-dependent reflection (ADR-0002), in-runtime provenance tagging (ADR-0003), replan-first recovery (ADR-0004) — a concrete orchestration technology needed to be chosen for it specifically, against that design rather than in the abstract.

Thoughts.md itself flagged this exact fork as unresolved when the component list was first drafted: "LangGraph and Temporal solve different layers here... complement or compete."

## Decision

LangGraph.

## Alternatives considered

- **Temporal.** A general-purpose durable execution engine — strong on retries, timeouts, and long-running-workflow guarantees. Rejected as the primary choice because the ReAct loop, stakes checks, and checkpoint structure from fig. 2 of the Agent Runtime design would all have to be built as custom logic on top of it; Temporal doesn't natively model an agentic reasoning graph.
- **Both together.** LangGraph for the reasoning/planning graph, Temporal underneath for durable execution across process restarts. Not rejected outright — noted as a real option — but not chosen now: more moving parts than the design currently needs, and durability-across-restarts hasn't yet been identified as a requirement this system has.
- **Custom state machine.** No external orchestration dependency, full control. Rejected because it rebuilds what LangGraph already provides (cycles, conditional edges, checkpointing) for no stated benefit.

## Consequences

- Fig. 2 of the Agent Runtime design maps directly onto LangGraph's execution model: Reason/Act/Observe as loop nodes, the "stakes?" and "subgoal met?" checks as conditional edges, Replan as a loop-back edge — the design was not changed to fit the tool; the tool was chosen because it already fit the design.
- Cross-process durability (surviving a restart mid-trajectory) is not covered by this choice. If that becomes a real requirement, the "both together" alternative above is the documented fallback, not a decision made from scratch.
- This is scoped to Agent Runtime only. It does not imply anything about which orchestration technology, if any, other components (e.g. a future Workflow Manager use elsewhere) should use.

## Related

- Full trajectory design: [Agent Runtime Design](https://claude.ai/code/artifact/89de3618-d0b8-44f3-af85-73dbfbd73df6).
- Build sequence: [Implementation Plan](https://claude.ai/code/artifact/2b28cf66-452e-4ed8-b7a1-8f11580325fa), Phase 2.
- Logged narratively in `../checkpoint.md`.
