# 0016 — Circuit breaker scope: per tool

**Status:** Accepted — 2026-08-26
**Component:** Reliability & Resilience (15)

## Context

Once ADR-0015 routes loop/cascade failures to a circuit breaker instead of Recovery Manager, Reliability & Resilience needed a decision on the breaker's scope: does it trip for the specific tool that's failing, or for the trajectory as a whole.

## Decision

Per tool: a failing tool gets temporarily marked unavailable; the trajectory continues if an alternative tool covers the same need, and only escalates to Decision & Policy if no alternative exists.

## Alternatives considered

- **Per trajectory.** If the trajectory as a whole is looping or cascading, the entire trajectory halts, regardless of which tool is involved. Rejected as the default because it discards partial progress and blocks unrelated tool calls within the same checkpoint that have nothing to do with the failing tool.

## Consequences

- Requires Tools & Environment (11, not yet designed) to expose enough structure for Reliability & Resilience to know which tools are interchangeable for a given need — this decision assumes that mapping exists or can be derived, without yet specifying how.
- A trajectory can now silently lose access to one tool mid-execution while continuing to look otherwise healthy; nothing yet surfaces "this trajectory completed without a tool it would normally have used" as a distinct, visible outcome.
- If no alternative tool exists, escalation to Decision & Policy still applies — this doesn't remove the trajectory-level halt case, it just makes it conditional rather than automatic.

## Related

- Full design: [Phase 0 Cross-Cutting Design](https://claude.ai/code/artifact/f9146a5d-2770-4f33-9b20-1c029a0cf22f), fig. 15.1.
- Depends on: [ADR-0015](0015-reliability-failure-classification-supersedes-0004.md).
- Logged narratively in `../checkpoint.md`.
