# 0015 — Failure classification revises ADR-0004

**Status:** Accepted — 2026-08-26. **Supersedes [ADR-0004](0004-agent-runtime-replan-first-recovery.md) in part.**
**Component:** Reliability & Resilience (15)

## Context

ADR-0004 set Agent Runtime's default recovery behavior to "always attempt replan first," and named its own alternative — routing by failure class — as a candidate for revision once Reliability & Resilience was designed, since that alternative assumed a Failure Classifier that didn't exist yet. New research (*Real-Time Detection and Repair of LLM Agent Failures*, 2026) has since shown that blind retry is not uniformly safe: retries recover some failure classes (e.g. goal drift) but loop and tool-cascade failures escalate under retry rather than resolve, and circuit breakers exit loop traps meaningfully faster than continued retrying does.

Reliability & Resilience's Failure Classifier now exists as a real design, which was the condition ADR-0004 itself set for reconsidering its default.

## Decision

Revise ADR-0004: Failure Classifier sits in front of Recovery Manager. Only genuinely transient failures are routed to Recovery Manager for replanning, as ADR-0004 originally specified. Loop and cascade patterns are routed to a circuit breaker instead (see [ADR-0016](0016-reliability-circuit-breaker-per-tool.md)), not to replan.

## Alternatives considered

- **Leave ADR-0004 unchanged, add circuit breaking only as a backstop behind it.** Replan-first stays the default everywhere; a circuit breaker only intervenes if replanning itself starts looping. Rejected because the new research specifically found that applying retry *before* attempting to classify is what makes loop and cascade failures worse — a backstop that only engages after replanning has already made the pattern worse doesn't prevent the harm the classification is meant to prevent.

## Consequences

- Agent Runtime's fig. 2 (Reason → Act → Observe → Recovery Manager → Replan) is now preceded by a classification step that didn't exist when that diagram was drawn. Recovery Manager's own internal logic is unchanged — see the addendum on the Agent Runtime Design artifact.
- ADR-0004 is not deleted or rewritten; it is superseded in part. Its core decision (replan-first, bounded budget, escalate when exhausted) still governs the transient-failure branch exactly as written. Only the "always" in "always attempt replan first" no longer holds unconditionally.
- Whether "loop or cascade pattern" can be detected reliably, and how quickly, is not yet designed — this ADR settles the routing decision, not the classifier's detection mechanism.

## Related

- Full design: [Phase 0 Cross-Cutting Design](https://claude.ai/code/artifact/f9146a5d-2770-4f33-9b20-1c029a0cf22f), fig. 15.1.
- Supersedes: [ADR-0004](0004-agent-runtime-replan-first-recovery.md).
- Logged narratively in `../checkpoint.md`.
