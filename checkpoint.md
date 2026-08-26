# Checkpoint — Portfolio Agent

`loop.md` is the code-phase counterpart to `design-framework.md` — what a coding subagent follows per component, once implementation starts. Written 2026-08-26, not yet used by any subagent.

## Going parallel: orchestration, blueprint, roadmap (2026-08-26)

- **Parallel execution, mitigated by an interface-first blueprint.** User leans parallel (Codex CLI + Broc CLI, roughly 50/50) and asked for the consequences and a mitigation. Decision: freeze `src/` (the blueprint) as the interface contract before any parallel coding starts; any change to a shared signature (`infrastructure.py`, `cross_cutting/*`) goes through loop.md step 2, not a silent edit. Coding can start on all 18 components in parallel against the frozen stubs; integration order still follows the real dependency graph. Full reasoning and the parallel-safe groupings: `orchestration.md`.
- **This session is the master reviewer**, across both tools. Checklist (signature match, ADR compliance, diagram fidelity, cross-cutting wiring, failure-mode test coverage, cross-component contradiction check, outstanding draft ADRs) and PASS/FAIL/CONTRADICTION report shape: `orchestration.md`.
- **Blueprint built**: `src/` — 18 component stubs, `infrastructure.py` (component 18's interface), `cross_cutting/{reliability,observability,security}.py` (components 15–17). No implementation logic; every signature traces to an ADR or a Component Whiteboards capability. Syntax-checked, all files parse. Index: `src/README.md`.
- **Roadmap built**: `roadmap.md` (rationale) and `roadmap.csv` (import-ready — Jira/Linear/Trello). Groups A–G by integration dependency, flags the 4 fully-designed components (05, 06, 09, 10) as lower-friction starting points, and keeps the three still-open items (Mem0-vs-Supermemory, Security & Privacy's authority granularity, Learning & Evaluation's scope) as visible blocking tasks rather than resolved dependencies.

## GitHub repo (2026-08-26)

- **https://github.com/Ankush82/portfolio-agent** — public, personal account. User chose public over the private default this session recommended (unresolved design decisions and internal reasoning are visible); their call.
- Local repo is scoped to the `Portfolio Agent` folder only — the home directory itself sits inside its own separate, uninitialized git repo; the two are deliberately not connected.
- 40 GitHub issues created from `roadmap.csv`: 8 epics (#33–40), 32 stories/tasks (#1–32), each epic body a checklist linking its children by issue number. Labels: `epic`, `story`, `task`, `blocked` (the two open-decision tasks), `done`. SETUP-1–4 and their epic-children-complete state closed automatically; EPIC-0 stays open since SETUP-5 (freeze confirmation) is still To Do.
- Script used: not kept in the repo (scratchpad only) — it's a one-shot CSV-to-issues generator, re-run `roadmap.csv` → GitHub manually if the roadmap changes significantly rather than assuming the script still matches.

## Blueprint made runnable and traced (2026-08-26)

- **Standing rules adopted from this point on**: the Anti-Slop Engineering Rules the user gave verbally (single clear intent per change, prefer modifying over rewriting, no dead code/TODOs/placeholders beyond what's designed, descriptive naming over comments, comments explain why not what, small independently reviewable commits). Applied to this change itself: 3 separate commits (tracing foundation, mechanical stub conversion, driver script), each a one-sentence intent.
- **Orchestration model going forward**: user asked to be spawning Sonnet subagents to do the actual work and stay in an orchestrating/reviewing role, rather than editing files directly. Used 5 subagents this round (4 parallel for the stub conversion, 1 for the trace driver); this session's own role was defining the pattern once (observability.py's `traced()`), briefing the subagents, and reviewing/functionally testing their output before committing.
- **Tradeoff, explained per the user's rule**: rather than inventing a new tracing utility, implemented the `traced()`/`Span` mechanism that already existed as a stub in `cross_cutting/observability.py` (component 16's own design). Reused an existing designed abstraction instead of adding a parallel one.
- **What changed**: every method across all 18 components, `infrastructure.py`, and `cross_cutting/{reliability,security}.py` now does `with traced("Class.method"): return <placeholder>` instead of `raise NotImplementedError` — still zero business logic, but the whole blueprint is importable, instantiable, and callable end to end. `infrastructure.py` gained `StubInfrastructure`, a concrete traced implementation of the `Infrastructure` Protocol (Protocols themselves aren't instantiated, so this was necessary to make the interface usable at all).
- **Verified, not assumed**: functionally exercised all 194 stub methods (not just `ast.parse`) — zero real errors. `src/run_trace.py` runs one simulated trajectory (User & Portfolio -> Agent Runtime -> Tools & Environment, per Agent Runtime Design fig. 1) end to end and produces a readable `trace.log`; re-ran it independently to confirm before committing. `trace.log` itself is gitignored (a run artifact, not source).
- **No TypeScript existed in the repo** — user asked for it removed; confirmed via search, nothing to do.
- **Explicitly not implemented, per the user's own instruction to just note it**: the architecture should eventually support scaling from a single simple agent up to hundreds of agents deployed in the cloud (a "software factory"), and the system should eventually be self-improving. Nothing was built toward this now — noted here so it isn't forgotten, and so future design choices (e.g. keeping `Infrastructure` as an injected interface, keeping each component in its own module) stay compatible with it without adding complexity today.

## Ports & Adapters formalized across all 18 components (2026-08-26)

- **Question asked directly**: whether the blueprint's structure used any deliberate design pattern. Honest answer at the time: package-by-component (implicit) plus a Ports & Adapters seam that only existed in `infrastructure.py` — not applied consistently, and never presented as a tradeoff the way other decisions in this project have been.
- **Decision: formalize it everywhere.** User picked "all 18 components" over the narrower option (just Memory and Agent Runtime, the two with an actual technology decision) after a scope-check on an ambiguous "all 10" — confirmed via a direct follow-up question rather than guessed.
- **Pattern**: every capability class `Foo` split into `Foo(Protocol)` (the port — signatures and docstrings, `...` bodies) and `StubFoo` (the adapter — the same traced no-op behavior that used to live directly on `Foo`). Exactly the shape `infrastructure.py`'s `Infrastructure`/`StubInfrastructure` already had; generalized, not reinvented. `Span` and `traced()` in `observability.py` were left untouched — real working code, not a stub.
- **Scale**: 37 Protocol/Stub pairs across 18 files. Done by 6 parallel subagents (grouped by file, not by hand), reviewed afterward: full syntax check, a Protocol-count-equals-Stub-count check per file, and a real functional test instantiating and calling all 139 `Stub*` methods — zero errors. `run_trace.py` updated to import the renamed `Stub*` classes and re-verified end to end.
- **Why this earns its keep now rather than being premature abstraction**: Memory (ADR-0010, Mem0 vs. Supermemory) and Agent Runtime (ADR-0009, LangGraph) both have a real pending or made technology choice that this seam exists to protect. The other 16 don't yet — applying it there anyway was the user's explicit call, not something this session decided on its own.

Every engineering decision below also has a standalone ADR in [`adr/`](adr/README.md) (context, alternatives considered, consequences). This file is the narrative log of the whole process, including scoping choices that aren't architecture decisions; `adr/` is the subset worth citing on its own.

Running log of decisions, approaches, and reasons for the low-level design pass that follows `portfolio_ai_three_literature_reviews.md`. Append-only. Each entry states the reason, not just the outcome, so it's readable cold later.

## Process rules (set 2026-08-25)

- The literature review informs design; technology selection stays deferred (the review's own last section says this explicitly).
- No more than 3 components get a literature-informed low-level design pass right now — the rest wait for a later round.
- One component at a time: design it fully, user confirms, only then move to the next.
- Wherever the literature surfaces a real tradeoff (e.g. adaptive vs. unconditional retrieval, interleaved vs. planned reasoning), Claude presents the options and asks — it does not pick.
- Each component's design must include a knowns/unknowns failure framework, not just sub-components / capabilities / core objects / interfaces / candidate tooling.

## Literature → component mapping (observed, not decided)

`portfolio_ai_three_literature_reviews.md` names three priority areas. Mapped onto the 18-component list from `Thoughts.md`:

1. **Retrieval + Evidence-Grounded Reasoning** → Retrieval & Context (05) + Evidence & Verification (09). Analysis & Reasoning (08) borders it too, since ALCE's claim-to-evidence chain runs through Analysis.
2. **Agent Runtime Reliability + Evaluation** → Agent Runtime (10). Learning & Evaluation (14) borders it too, via Reflexion's feedback loop.
3. **Memory for Long-Lived Agents** → Memory (06). This is the literature's actual "knowledge-retention" pillar — Knowledge & Entity Model (04) is not covered by this review.

## Open questions

- Still deferred: whether Learning & Evaluation (14) rides with a future round or gets its own — not resolved yet.

## Scaling the design framework to all 18 components

- **Grounding method for the 14 components with no existing literature review: mixed.** Literature search first, per component; if nothing substantive turns up, fall back to an engineering discussion instead. Decided 2026-08-26.
- **Order for the remaining 16: finish the already-grounded pair first** (Retrieval & Context / Evidence & Verification — literature ready since the original review, steps 3–5 still pending), then the other 14 in build-sequence order from the Implementation Plan. Decided 2026-08-26.

## Decisions

- **Component to start with: Agent Runtime.** Reason: user named it first; matches "Agent Runtime Reliability + Evaluation" literature (AgentBench, ReAct, Reflexion, AgentDojo) most directly. Decided 2026-08-25.

- **Reason/act loop: hybrid with checkpoints**, not plan-then-execute and not fully interleaved ReAct. Planner sets subgoals (checkpoints) upfront; ReAct-style reason → act → observe interleaves within each checkpoint. Reason: bounds how far a bad reasoning step can drift before anything checks it, without losing ReAct's ability to adapt to what was just observed. Decided 2026-08-25.

- **Reflection timing: depends on stakes**, not trajectory-only and not step-level-always. Step-level reflection only for high-stakes steps (the ones Policy & Safety would flag anyway); trajectory-level reflection (Reflexion's own pattern) always runs at the end regardless. Decided 2026-08-25.

- **Adversarial-input defense: lives inside Agent Runtime**, not fully delegated to Security & Privacy / Policy & Safety. Task Manager and Executor tag ingested content as untrusted at the point it enters the loop, before it can be reasoned over as an instruction. Reason: AgentDojo's threat model is specifically about the runtime's own reasoning step conflating data with instructions, so the tagging has to happen where the conflation risk actually is. Decided 2026-08-25.

- **Recovery trigger: always attempt replan first**, not fail-closed and not failure-class-routed. Recovery Manager retries/replans autonomously within a bounded retry budget; escalates to Decision & Policy only once that budget is exhausted. Decided 2026-08-25.

- **Component 2 of this round: Memory.** Reason: user's choice when asked which of the two remaining grounded components (Memory, Retrieval & Context / Evidence & Verification) to run through the design-framework loop next. Decided 2026-08-26.

- **Memory management: active (MemGPT-style)**, not passive. Memory Manager curates a bounded working set on its own logic, evicting to archive rather than only responding to Store/Retrieve calls. Decided 2026-08-26.

- **Memory structure: linked network (A-MEM)**, not independent records. A new memory is analyzed against existing memories and gets explicit links at write time, rather than relatedness only being discovered later at search time. Decided 2026-08-26.

- **Memory poisoning defense: quarantine at write**, not trust-weighted at read. A memory derived from untrusted or unverified content is flagged and held back before it becomes usable memory; the gate sits at Store, not Retrieve. Decided 2026-08-26.

- **Memory scope: structural partition**, not single-store-with-metadata. User-specific memory and globally shared memory live in physically separate stores; a query has to specify which it's asking. Decided 2026-08-26.

## Design artifacts

- **Agent Runtime Design** — fig. 1 trajectory overview, fig. 2 inside-one-checkpoint detail, knowns/unknowns failure framework. All four decisions drawn into fig. 2 as one mechanism.
- **Memory Design** — fig. 1 write path (evaluate → structure → link → quarantine gate → track provenance/confidence/freshness → scoped store), fig. 2 read path and working set (retrieval, active management, eviction, periodic consolidation feeding back into fig. 1's linking step), knowns/unknowns failure framework. All four decisions drawn in.

Both components have completed steps 1–5 of the design framework. **Confirmed by user 2026-08-26.** Third and last component of this round: Retrieval & Context / Evidence & Verification, not yet started.

## Technology decisions

- **Agent Runtime → LangGraph.** Reason: its graph-based execution model with cycles and conditional edges maps directly onto the checkpoint + ReAct loop from fig. 2 of the Agent Runtime design — Reason/Act/Observe as loop nodes, the stakes check and subgoal-met check as conditional edges, replan as a loop-back edge. Chosen over Temporal alone, over running both together, and over a custom state machine. Decided 2026-08-26.

- **Memory → purpose-built memory layer (Mem0 or Supermemory), not yet chosen between the two.** Reason: preferred over building a unified Postgres+pgvector store by hand or wiring three specialized systems together. Still open: neither product has been checked against the four specific Memory decisions (active working-set management, A-MEM-style linking, quarantine-at-write, structural user/shared partition) — that comparison is real follow-up work before Phase 5 of the build sequence, not settled yet. Decided 2026-08-26.

## Retrieval & Context / Evidence & Verification (components 05, 09)

- **Component 3 of the original round: Retrieval & Context / Evidence & Verification.** Grounding already done (portfolio_ai_three_literature_reviews.md, section 1: Self-RAG, CRAG, ALCE, RAGTruth). Decided to finish this pair before starting fresh grounding on the other 14 — 2026-08-26.

- **Adaptive retrieval (Self-RAG)**, not unconditional. Retrieval & Context includes a "should I retrieve?" gate; some queries proceed on existing context/memory without a fetch. Decided 2026-08-26.

- **Corrective retrieval (CRAG)**, not pass-through. A Retrieval Evaluator judges sufficiency; insufficient retrieval triggers bounded corrective/external search, and "no useful evidence found" is a legitimate terminal state once the retry budget is exhausted, not a silent failure. Decided 2026-08-26.

- **Evidence requirement: mandatory per claim (ALCE)**, not graded. A claim without supporting evidence is blocked from reaching Decision & Policy — logged, not forwarded, not down-weighted-and-passed. Decided 2026-08-26.

- **Contradictory evidence: resolved automatically**, not surfaced as its own state. Weight by source reliability and freshness, pick the higher-confidence side, pass one answer downstream. Decided 2026-08-26.

Full design (fig. 1 retrieval path, fig. 2 evidence path, knowns/unknowns) published: [Retrieval & Evidence Design](https://claude.ai/code/artifact/afae265c-32a2-460f-b537-24a4cfc736d4). This closes the original three-pillar literature round — all three grounded components (Agent Runtime, Memory, Retrieval & Evidence) have completed steps 1–5.

## Phase 0 — cross-cutting (components 15, 16, 17, 18)

- **Pairing: all three literature-grounded components together** (Reliability & Resilience, Observability & Governance, Security & Privacy), one round, same shape as Retrieval & Evidence. System Infrastructure done in the same pass via engineering discussion. Decided 2026-08-26.
- Grounding for 15/16/17 found via literature search: self-healing orchestrators, real-time failure detection/circuit breakers, an agentic-web observability gap analysis, a 3-tier observability framework, Agent Security Bench (ICLR 2025), multi-agent trust exploitation research, AIRGuard. Full citations in the design artifact.

- **Reliability & Resilience: failure classification revises ADR-0004.** New research shows blind retry makes loop/cascade failures worse, not better. Failure Classifier now routes: transient failures still go to Recovery Manager/Replan (ADR-0004's mechanism, unchanged); loop/cascade patterns trip a circuit breaker instead. See ADR-0015, which supersedes ADR-0004 in part. Decided 2026-08-26.
- **Circuit breaker scope: per tool**, not per trajectory. A failing tool is blocked temporarily; the trajectory continues if an alternative tool exists, escalates to Decision & Policy if not. Decided 2026-08-26.
- **Observability & Governance: infrastructure-level tier only**, not all three tiers. Logs, traces, cost/latency now; post-hoc evaluation and predictive monitoring wait for real trajectory data to exist. This component now has to actually track the drift signals Agent Runtime, Memory, and Retrieval & Evidence's failure frameworks already promised it would watch. Decided 2026-08-26.
- **Security & Privacy: peer-agent output untrusted by default**, extending ADR-0003's document-tagging mechanism to Delegation Manager's returns. New research shows agents trust peer agents even when they'd refuse an identical direct instruction. Decided 2026-08-26.
- **System Infrastructure: unified, managed stack, built to scale from day one.** Postgres (managed) for relational + queue + pgvector, Redis (managed) for cache, S3-compatible object storage, cloud secret manager, API Gateway in front — every component talks through an interface, not directly to a store, so the stack can be split apart later without a rewrite. Decided 2026-08-26, via engineering discussion (no literature exists for this one).

Full design (fig. 15.1 failure classification, fig. 16.1 tracing pipeline, fig. 17.1 boundary gate, fig. 18.1 infrastructure stack) published: [Phase 0 Cross-Cutting Design](https://claude.ai/code/artifact/f9146a5d-2770-4f33-9b20-1c029a0cf22f).

Still open: authority-check granularity for Security & Privacy (per task vs. per tool call) — surfaced during research but not asked yet, not blocking anything designed so far.

## Scaling to the remaining 10 components

Next, per Implementation Plan build-sequence order: User & Portfolio (01) — Phase 1 — then Tools & Environment (11), Data & Sources (02), Data Processing & Quality (03), Knowledge & Entity Model (04), Event & Observation (07), Analysis & Reasoning (08), Decision & Policy (12), Interaction & Notification (13), and the still-unresolved Learning & Evaluation (14). Mixed grounding continues: literature search first, engineering discussion if nothing substantive turns up.

## Implementation plan

Full build-sequence + technology plan published as an Artifact, "Implementation Plan." Covers all 18 components: concrete tech + build detail for Agent Runtime and Memory (the two with completed designs), and a dependency-ordered build sequence with design-readiness status for the other 16 — no technology invented for designs that don't exist yet.
