# 0017 — Observability scope: infrastructure-level tier only

**Status:** Accepted — 2026-08-26
**Component:** Observability & Governance (16)

## Context

The *Agent Delivery Engineering Predictive Reliability Framework* describes three progressive tiers of observability for LLM agent systems: infrastructure-level monitoring (logs, traces, cost), post-hoc evaluation and reflection, and real-time predictive monitoring. Separately, Agent Runtime, Memory, and Retrieval & Evidence's own knowns/unknowns grids already named "Observability & Governance watches for drift" as their stated mitigation for unnamed failure modes — meaning this component's scope decision has direct consequences for three already-published designs. Observability & Governance needed a decision on which tier(s) to build first.

## Decision

Infrastructure-level only, for now: structured logs, distributed traces, and cost/latency tracking on every call. Post-hoc evaluation and real-time predictive monitoring are explicitly out of scope for this pass.

## Alternatives considered

- **All three tiers from the start.** Built together, since the source framework designed them as one system. Rejected because post-hoc evaluation and predictive monitoring both require trajectory data that doesn't exist yet — the system hasn't run. Building evaluation logic against data that isn't being produced yet is building on a premise this project isn't at.

## Consequences

- The specific drift-watching promises made by three earlier designs are now this component's obligation to actually track: Agent Runtime's cost/latency/checkpoint-count per trajectory, Memory's corroboration rate and eviction/re-retrieval thrashing, and Retrieval & Evidence's block rate, corrective-retrieval rate, and repeated source disagreement. None of those are evaluation or prediction — all are infrastructure-tier metrics, so this scope decision can actually deliver on them.
- Post-hoc evaluation (comparing prediction to outcome) and predictive monitoring are deferred, which means they are also deferred for Learning & Evaluation (14, not yet designed) — that component will need infrastructure-tier data to already exist before its own design can assume it.
- No mechanism yet exists for someone to actually look at what's being logged and traced; this component produces the data, not the dashboard or the alerting on top of it.

## Related

- Full design: [Phase 0 Cross-Cutting Design](https://claude.ai/code/artifact/f9146a5d-2770-4f33-9b20-1c029a0cf22f), fig. 16.1.
- Logged narratively in `../checkpoint.md`.
