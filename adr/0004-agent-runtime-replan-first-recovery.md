# 0004 — Recovery trigger: always attempt replan first

**Status:** Partially superseded by [ADR-0015](0015-reliability-failure-classification-supersedes-0004.md) — 2026-08-26. The bounded-replan mechanism below is unchanged; "always" no longer holds unconditionally, since ADR-0015 routes loop/cascade failures to a circuit breaker instead.
**Component:** Agent Runtime (10)

## Context

A step inside a checkpoint can fail outright (tool error, timeout) or return a low-confidence result. Agent Runtime needed a default rule for what Recovery Manager does in that situation, before it ever escalates to Decision & Policy.

## Decision

Always attempt replan first: Recovery Manager retries and replans autonomously within a bounded retry budget; it escalates to Decision & Policy only once that budget is exhausted.

## Alternatives considered

- **Fail closed by default.** Any failure or low-confidence step escalates immediately; the runtime doesn't try to route around it on its own. Rejected as the default because it would push routine, recoverable failures (a flaky tool call, a transient timeout) up to Decision & Policy far more often than necessary.
- **Depends on failure class (Failure Classifier, component 15).** Transient/tool failures auto-retry, reasoning/evidence failures escalate. Rejected for now specifically because component 15 (Reliability & Resilience) hasn't been designed yet — routing by failure class assumes a classifier that doesn't exist. Worth reconsidering once 15 is designed.

## Consequences

- Recovery Manager needs a concrete retry budget and a way to detect when replanning itself is failing (replan → fail → replan, without ever tripping the intended limit) — flagged as known-unknown in the failure framework, not yet resolved.
- Escalation to Decision & Policy becomes rarer and later than the "depends on failure class" alternative would have produced, which trades faster autonomous recovery for a shorter list of what actually reaches human/policy attention.
- ~~This decision is a candidate for revision once component 15 (Reliability & Resilience) is designed — see the alternative above.~~ Resolved: see [ADR-0015](0015-reliability-failure-classification-supersedes-0004.md).

## Related

- Full trajectory design: [Agent Runtime Design](https://claude.ai/code/artifact/89de3618-d0b8-44f3-af85-73dbfbd73df6), fig. 1 (the escalation branch) and fig. 2 (Recovery Manager → Replan → Escalate).
- Logged narratively in `../checkpoint.md`.
