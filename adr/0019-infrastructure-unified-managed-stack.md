# 0019 — System Infrastructure: unified, managed stack

**Status:** Accepted — 2026-08-26
**Component:** System Infrastructure (18)

## Context

Unlike the other 17 components, System Infrastructure has no research literature to ground it in — it's classic distributed-systems engineering (databases, queues, cache, object storage, scheduling), not a research question. Per the design framework's own "mixed" grounding rule, this component was grounded in a direct engineering discussion with the user instead: deployment scale, unified-vs-specialized stack, and managed-vs-self-hosted.

## Decision

A unified, managed stack, built to scale from day one:

- **Postgres (managed — e.g. Neon, Supabase)** for relational data, a table-based queue, and pgvector if needed.
- **Redis (managed — e.g. Upstash)** for cache.
- **S3-compatible object storage** for documents.
- **The cloud provider's secret manager** for secrets.
- An **API Gateway** in front, with every other component talking to System Infrastructure through an interface, never directly to a specific store.

## Alternatives considered

- **Best tool per concern** (Kafka/Redpanda for the event bus, a dedicated queue, Postgres only for relational data). Rejected for now: more systems to operate and pay for, for capability this project doesn't yet have the traffic to need.
- **Self-hosted** (Docker/Kubernetes, own instances). Rejected for now: more operational burden while the rest of the system is still being designed, in exchange for control and cost-at-scale benefits that don't yet apply.
- **Personal/prototype scale.** Explicitly rejected by the user in favor of planning for scale from day one, despite the system not yet having real users.

## Consequences

- "Plan for scale" and "unified stack" are reconciled through the interface boundary, not through the technology choice itself: every component talks to System Infrastructure through an interface, so Postgres-as-queue can later be replaced (e.g. by Kafka or Redpanda) behind that interface without changing any caller. The scaling plan is architectural, not a promise about the initial stack's ceiling.
- Availability now depends on third-party managed-service uptime, not just this system's own code — named as a risk, not yet designed around (no multi-region, backup/restore, or disaster-recovery decision has been made).
- Postgres-as-queue has a real throughput ceiling that hasn't been quantified; nothing in this decision specifies the volume at which it becomes a problem, only that a replacement path exists when it does.
- This is the second component, after Agent Runtime (ADR-0009) and Memory (ADR-0010), with an actual technology decision rather than a design-only decision.

## Related

- Full design: [Phase 0 Cross-Cutting Design](https://claude.ai/code/artifact/f9146a5d-2770-4f33-9b20-1c029a0cf22f), fig. 18.1 and its risks list.
- Logged narratively in `../checkpoint.md`.
