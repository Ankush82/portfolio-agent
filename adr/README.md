# Architecture Decision Records

This folder logs every architecturally significant decision made in the Portfolio Agent design phase, one file per decision, in the order they were made. Each ADR states the context that forced the decision, what was decided, what else was considered and why it wasn't chosen, and what the decision costs going forward.

These are the same decisions logged narratively in [`../checkpoint.md`](../checkpoint.md); this folder holds the same information in a standard, one-decision-per-file format for later reference. Every ADR below is **Accepted** except where the index notes otherwise (a **Proposed** ADR is a draft, raised per `loop.md` step 2 when implementation surfaced a gap no prior ADR settled — it is not yet a binding decision); none has been reversed.

Scope: this covers engineering/architecture decisions only, not process choices like "which component do we design next" — those stay in `checkpoint.md`, since they're sequencing, not architecture.

## Index

| ADR | Decision | Component |
|---|---|---|
| [0001](0001-agent-runtime-hybrid-checkpoint-loop.md) | Reason/act loop: hybrid with checkpoints | Agent Runtime (10) |
| [0002](0002-agent-runtime-stakes-dependent-reflection.md) | Reflection timing: depends on stakes | Agent Runtime (10) |
| [0003](0003-agent-runtime-in-runtime-adversarial-input-defense.md) | Adversarial-input defense lives inside Agent Runtime | Agent Runtime (10) |
| [0004](0004-agent-runtime-replan-first-recovery.md) | Recovery trigger: always attempt replan first | Agent Runtime (10) |
| [0005](0005-memory-active-working-set-management.md) | Memory management: active (MemGPT-style) | Memory (06) |
| [0006](0006-memory-linked-network-structure.md) | Memory structure: linked network (A-MEM) | Memory (06) |
| [0007](0007-memory-quarantine-at-write.md) | Memory poisoning defense: quarantine at write | Memory (06) |
| [0008](0008-memory-structural-partition.md) | Memory scope: structural partition | Memory (06) |
| [0009](0009-agent-runtime-technology-langgraph.md) | Agent Runtime technology: LangGraph | Agent Runtime (10) |
| [0010](0010-memory-technology-purpose-built-layer.md) | Memory technology: purpose-built memory layer (Mem0 or Supermemory) | Memory (06) |
| [0011](0011-retrieval-adaptive-gate.md) | Adaptive retrieval (Self-RAG) | Retrieval & Context (05) |
| [0012](0012-retrieval-corrective-retrieval.md) | Corrective retrieval (CRAG) | Retrieval & Context (05) |
| [0013](0013-evidence-mandatory-per-claim.md) | Evidence requirement: mandatory per claim (ALCE) | Evidence & Verification (09) |
| [0014](0014-evidence-automatic-contradiction-resolution.md) | Contradictory evidence: resolved automatically | Evidence & Verification (09) |
| [0015](0015-reliability-failure-classification-supersedes-0004.md) | Failure classification revises ADR-0004 | Reliability & Resilience (15) |
| [0016](0016-reliability-circuit-breaker-per-tool.md) | Circuit breaker scope: per tool | Reliability & Resilience (15) |
| [0017](0017-observability-infrastructure-tier-only.md) | Observability scope: infrastructure-level tier only | Observability & Governance (16) |
| [0018](0018-security-peer-agent-untrusted-by-default.md) | Peer-agent output: untrusted by default | Security & Privacy (17) |
| [0019](0019-infrastructure-unified-managed-stack.md) | System Infrastructure: unified, managed stack | System Infrastructure (18) |
| [0020](0020-security-authorize-interim-default.md) | Authorize interim default: fail-open, logged, per-call (**Proposed**, not yet Accepted) | Security & Privacy (17) |
| [0021](0021-agent-runtime-llm-provider-interim.md) | Agent Runtime LLM provider interim: injectable, non-cognitive placeholder (**Proposed**, not yet Accepted) | Agent Runtime (10) |

ADR-0004 is partially superseded by ADR-0015 — see both for the full picture; superseded ADRs stay in the folder rather than being deleted.

## Template

Each ADR follows the same shape: Status, Context, Decision, Alternatives considered, Consequences, Related. New ADRs should follow it too, so the folder stays easy to scan.
