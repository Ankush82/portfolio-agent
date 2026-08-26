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
| [0010](0010-memory-technology-purpose-built-layer.md) | Memory technology: Mem0 chosen; per-decision fit check now resolved, three of four decisions implemented directly against DefaultInfrastructure | Memory (06) |
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
| [0022](0022-user-portfolio-broker-connector-interface.md) | Broker connector: injectable Protocol, broker data untrusted by default | User & Portfolio (01) |
| [0023](0023-user-portfolio-broker-api-choice-interim.md) | Broker/aggregator API choice interim: placeholder connector (**Proposed**, not yet Accepted) | User & Portfolio (01) |
| [0024](0024-tools-environment-registry-and-dispatch-mechanism.md) | Registry/dispatch mechanism: parallel invocable registry, capability-tag matching, empty by default | Tools & Environment (11) |
| [0025](0025-tools-environment-interchangeability-closes-circuit-breaker-gap.md) | Tool interchangeability mapping wired into CircuitBreaker.find_alternative() and DefaultRecoveryManager | Tools & Environment (11), Reliability & Resilience (15), Agent Runtime (10) |
| [0026](0026-data-sources-real-mechanism.md) | Data & Sources real mechanism: single-table persistence, provenance/timestamp/reliability wiring, SourceFetcher seam | Data & Sources (02) |
| [0027](0027-data-sources-fetch-provider-interim.md) | Data & Sources fetch provider interim: injectable, non-fetching placeholder (**Proposed**, not yet Accepted) | Data & Sources (02) |
| [0028](0028-memory-mem0-llm-embedding-provider-interim.md) | Mem0 LLM/embedding provider interim: deferred to DefaultInfrastructure (**Proposed**, not yet Accepted) | Memory (06) |
| [0029](0029-evidence-linking-relatedness-and-search-mechanism.md) | Evidence linking: Jaccard relatedness rule, MemoryManager/ContextPack search mechanism | Evidence & Verification (09) |
| [0030](0030-evidence-contradiction-detection-and-resolution-weighting.md) | Contradiction detection rule (topic-gated field conflict) and resolution weighting (reliability × freshness) | Evidence & Verification (09) |
| [0031](0031-claim-verification-citation-completeness-and-confidence-scoring.md) | Claim verification: independent-source citation completeness and confidence formula | Evidence & Verification (09) |
| [0032](0032-data-processing-quality-real-mechanism.md) | Data Processing & Quality real mechanism: structural parse/extract, rule-based normalize/transform/dedup/validate, computed quality score, real staleness check, Infrastructure-backed lineage | Data Processing & Quality (03) |
| [0033](0033-retrieval-gate-evaluator-and-retriever-real-mechanism.md) | Retrieval & Context real mechanism: keyword-overlap adaptive gate, retriever wiring to components 02/04, reliability/coverage/freshness sufficiency evaluator, evaluator-backed context assembly | Retrieval & Context (05) |
| [0034](0034-retrieval-corrective-external-search-provider-interim.md) | Corrective retrieval external search provider interim: injectable, empty-by-default placeholder (**Proposed**, not yet Accepted) | Retrieval & Context (05) |

ADR-0004 is partially superseded by ADR-0015 — see both for the full picture; superseded ADRs stay in the folder rather than being deleted.

## Template

Each ADR follows the same shape: Status, Context, Decision, Alternatives considered, Consequences, Related. New ADRs should follow it too, so the folder stays easy to scan.
