# Checkpoint — Portfolio Agent

`loop.md` is the code-phase counterpart to `design-framework.md` — what a coding subagent follows per component, once implementation starts. Written 2026-08-26, not yet used by any subagent.

## Agent Runtime (10): real LangGraph orchestration (2026-08-26)

- **Built, following loop.md exactly**: six `Default*` classes added to `src/components/c10_agent_runtime.py` alongside the existing `Stub*` classes (untouched), plus the actual LangGraph graph fig. 2 was designed for (ADR-0009).
  - `DefaultTaskManager` — real lifecycle backed by an instance dict keyed by a real `uuid.uuid4()` id; `pause`/`resume`/`terminate` raise `KeyError` on an unknown task id rather than silently no-opping.
  - `DefaultStateManager` — real `get_state`/`update_state`, in-memory dict keyed by task id, deltas merged on update. Deliberately not wired to `Infrastructure` — System Infrastructure's real Postgres-backed store was landing in parallel this same round and wasn't ready; noted in the class docstring as the honest scope for this pass, not a gap.
  - `DefaultRecoveryManager` (ADR-0004, revised by ADR-0015) — builds a `FailureEvent`, classifies it via the real `DefaultFailureClassifier`, replans (returns the same `Checkpoint`, retry counter incremented) only for `FailureType.TRANSIENT` while budget remains; for `FailureType.LOOP_OR_CASCADE` or an exhausted budget, trips the named tool's `DefaultCircuitBreaker` (loop/cascade only) and raises `EscalationRequired` after `escalate()` logs via `DefaultAuditManager`. `EscalationRequired` is a small exception, not a new outcome dataclass — the success path still returns a plain `Checkpoint`.
  - `DefaultWorkflowManager` — `finalize()` assembles a real `TrajectoryOutcome` from the task and whatever checkpoints were actually passed in, and always calls the new `reflect_trajectory()` (ADR-0002, Reflexion pattern, unconditional) before returning; that method records a reflection via `DefaultAuditManager` rather than fabricating reflection content no reasoning backend produced.
  - `DefaultDelegationManager` — `delegate()` tags whatever a simulated sub-agent "returns" (there is no real sub-agent yet) as `UNTRUSTED` via `DefaultBoundaryGate.tag_provenance`, exercising ADR-0018 for real rather than just declaring it in a docstring.
  - `DefaultAgentCoordinator` — `coordinate()` runs fig. 1 for real: `Planner.plan_checkpoints()` → the compiled graph invoked once per checkpoint → `WorkflowManager.finalize()`.
- **`build_agent_runtime_graph()`** — the actual `langgraph.graph.StateGraph` for fig. 2. `LoopState` (a `TypedDict`) carries the checkpoint, the running history, retry count, and a done flag. Nodes: `reason`, `act` (tags its result `UNTRUSTED` before `observe` can see it — ADR-0003's Act→Observe edge, made real, not just documented), `observe`, `assess_stakes`, `reflect`, `recover`, `finish`. Conditional routing: after `observe`, failure routes to `recover`; otherwise to `assess_stakes`. `assess_stakes` routes to `reflect` when stakes are high (ADR-0002), else straight to the subgoal check; `reflect` always then hits the same subgoal check. The subgoal check (driven by the `reason` node's `checkpoint_complete` flag) routes back to `reason` (loop continues) or to `finish` (checkpoint complete). `recover` routes back to `reason` on a successful replan, or to `finish` (with `done=True`, failure recorded) on `EscalationRequired`.
  - **Judgment call**: the task's instructions describe the three-way "reason again / checkpoint complete / recover" branch as sitting directly after `observe`, and separately describe the stakes check as also sitting "after observe." Implemented as nested, not parallel: `observe` branches first on failure vs. not; the non-failure path goes through `assess_stakes` (and conditionally `reflect`) before reaching the same reason-again/checkpoint-complete decision the failure-free path was always going to reach. Both instructions hold simultaneously under this reading; a flatter version was considered and rejected because it would let stakes assessment happen even after a failure was already routed to `recover`, which fig. 2 doesn't call for.
- **The genuine gap (loop.md step 2), not papered over**: Agent Runtime's "Reason" and "assess stakes" steps require an LLM to mean anything, and this project has never chosen an LLM provider anywhere. `reason_fn: Callable[[dict], dict]` is injected into `build_agent_runtime_graph()` rather than any model call being hardcoded. `placeholder_reason_fn` is the concrete, explicitly non-cognitive stand-in shipped alongside it — deterministically reports "checkpoint complete" after exactly one reason/act/observe cycle and always reports low stakes, so the graph is genuinely runnable end to end without an LLM while being unmistakably not real reasoning. **[ADR-0021](adr/0021-agent-runtime-llm-provider-interim.md)** (Status: Proposed) documents this exactly like ADR-0020 did for `authorize()` — real decision now waiting on the user, not a sequencing item. Added to `adr/README.md`'s index and this file's Open questions.
- **`pyproject.toml`**: `langgraph>=0.2` added to top-level `dependencies`. `uv.lock` regenerated to match (LangGraph's transitive deps now real project dependencies, not just test-time `--with` packages).
- **Tests**: `tests/components/test_agent_runtime.py` (new `tests/components/__init__.py` alongside it — `tests/__init__.py` already existed from the System Infrastructure round, reused rather than recreated). 13 tests: `DefaultTaskManager` create/pause/resume/terminate including the unknown-id `KeyError` case; `DefaultRecoveryManager` transient-replan-decrements-budget, budget-exhausted-escalates, and a real 3-in-a-row same-component failure sequence against the actual `DefaultFailureClassifier` (not a reimplementation of its rule) driving a `LOOP_OR_CASCADE` escalation with the tool's circuit breaker verified tripped; `DefaultDelegationManager.delegate()` returns `UNTRUSTED`-tagged output; the compiled graph built with `placeholder_reason_fn`, run on one `Checkpoint`, asserting it actually completes with the state shape reflecting exactly one full reason/act/observe/assess_stakes cycle (not just "didn't crash"), plus a check that `act`'s output in the graph's own history is provenance-tagged before `observe`. All 13 passed (`PYTHONPATH=src uv run --python 3.11 --with langgraph --with pytest pytest tests/components/test_agent_runtime.py -v`). Full suite re-run after: 33 passed, 8 skipped (same 8 System Infrastructure skips as the prior round — no live Postgres/Redis daemon in this sandbox), zero new failures or conflicts with the parallel System Infrastructure round landing the same day.
- **Not touched, per instruction**: `src/infrastructure_postgres.py`, `docker-compose.yml` — System Infrastructure's own files from the parallel round.
- **Next**: a real `reason_fn` behind ADR-0021 once the user picks a provider; `DefaultStateManager` wiring to the now-real `Infrastructure` once that round is reviewed; `DefaultPlanner` (still `StubPlanner`-only — out of this task's stated scope, `Executor`'s reason/act/observe responsibilities are now carried by the graph instead, so a real `DefaultExecutor` was not built either, by design, not oversight).

## System Infrastructure (18): real Postgres + Redis implementation (2026-08-26)

- **Built, following loop.md exactly**: `DefaultInfrastructure` in `src/infrastructure_postgres.py` — the concrete implementation `infrastructure.py`'s own docstring already pointed at. Implements all 9 `Infrastructure` Protocol methods for real, each wrapped in `with traced("DefaultInfrastructure.<method>"): ...`, matching the `DefaultFailureClassifier`/`DefaultAuditManager`/`DefaultBoundaryGate` naming convention from the same implementation round.
  - `store`/`retrieve`/`query` → Postgres `records` table (JSONB, `data @> filters` containment match for query — deliberately not a general query DSL).
  - `publish`/`subscribe` → Postgres `queue_events` table. `subscribe` is honestly scoped as poll-once, not live push: it queries every currently-unconsumed event on a topic, calls the handler once per event, marks each consumed. Real push (LISTEN/NOTIFY + a background listener thread) is out of scope for this pass — stated plainly in the method's docstring.
  - `schedule` → Postgres `scheduled_tasks` table.
  - `cache_get`/`cache_set` → a real `redis` client directly (JSON-serialized values, so `Any` round-trips correctly, not just strings).
  - `get_secret` → `os.environ[name]`, docstring stating plainly this is a local-dev placeholder for the cloud secret manager ADR-0019 actually specifies — real cloud secret manager integration is not implemented now.
  - Schema (`records`, `queue_events`, `scheduled_tasks`) is created idempotently (`CREATE TABLE IF NOT EXISTS`) the first time a connection opens — no separate migration step yet. Constructor takes a Postgres DSN and Redis URL with localhost defaults matching `docker-compose.yml`; connections open lazily, so construction never touches the network.
  - **Judgment call, not an ADR gap**: the Protocol didn't say where `store`'s returned id comes from when the caller's record dict has no `"id"` key. Resolved by using `record["id"]` when present, else generating a uuid4 — documented in the method's own docstring rather than left implicit.
  - **Named tension, not silently resolved**: `infrastructure.py`'s `Infrastructure` Protocol docstring says "never read an environment variable... directly for anything credential-shaped (ADR-0019)," while this implementation's `get_secret` does exactly that. This was an explicit instruction for this round (env vars as the local-dev placeholder, cloud secret manager integration explicitly out of scope) — followed as directed, with the tension called out in the method's docstring and here, not swept under it.
- **`docker-compose.yml`** added at the repo root: `postgres` (postgres:16, named volume, port 5432, dev-only fixed credentials `portfolio_agent`/`portfolio_agent`/`portfolio_agent`) and `redis` (redis:7, port 6379) — local dev only, not the managed stack ADR-0019 specifies for production. Two services, nothing extra.
- **`pyproject.toml`**: `psycopg[binary]>=3.1` and `redis>=5.0` added to top-level `dependencies` (real runtime deps now, not dev-only). `uv.lock` regenerated to match.
- **`StubInfrastructure` untouched**, per instruction — stays the lightweight traced no-op double.
- **Tests**: `tests/test_infrastructure_postgres.py`, 11 tests. No live Postgres/Redis daemon in this sandbox (Docker installed, daemon not running), so `_postgres_available()`/`_redis_available()` probe with a short connect timeout and `@pytest.mark.skipif` skips the 8 tests that need a live service with a clear reason string — they don't fail and don't fake a pass. The other 3 (get_secret behavior, no-network-at-construction) need no live service and actually ran: 3 passed. Full suite after this change: 20 passed, 8 skipped. Real coverage on the skipped 8 (store/retrieve round trip, JSONB filter query, publish→subscribe delivery + consumed-marking, cache round trip) requires `docker-compose up -d` on a machine where Docker's daemon is running.
- **Next** unchanged from the prior round's note: Agent Runtime (10) needs the LangGraph dependency and its fig. 1/2 loop built as a real graph; Memory (06) needs Mem0 wired in behind the now-resolved vendor choice; the other 10 components remain whiteboard-only.

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

## Self-evolving harness — literature review only (2026-08-26)

- User described a new concern, not yet designed: a self-evolving build harness — spec-driven "contract" → agent factory → multi-perspective QA (developer / agent-decoding / browser) → deploy → monitor → breakage requeued → the harness itself learns across generations (their "G1, G2, G3"). Explicit instruction: literature review only, no implementation, no design decisions yet.
- Grounding written to `self-evolving-harness-literature-review.md` (not `blueprint.md` as literally requested — that name is already the code blueprint's index; flagged to the user, open to renaming). Seven areas: harness engineering (Weng 2026, the closest direct match to the user's own framing), spec-driven development & agent factories (MetaGPT, ADAS, AFlow), self-recursive/evolutionary improvement (Darwin Gödel Machine, AlphaEvolve, STOP, Red Queen Gödel Machine), multi-perspective QA (AHE's three pillars, FullStack-Agent, WebTestBench), context/graph engineering (ACE, MCE, code knowledge graphs), the deploy-monitor-requeue loop (ties to existing ADR-0015–0018), and — most load-bearing for this user's stated worry — the literature's own named failure modes (weak evaluators, diversity collapse, reward hacking) and how this project's existing ADR/Anti-Slop discipline already partially answers them.
- Not yet decided: whether this becomes a 19th component or extends Agent Runtime's design. Open, per the review's own closing note.

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
- Agent Runtime has never had an LLM provider chosen for its own reasoning (the "Reason" and "assess stakes" steps in fig. 2) — not in any ADR, not anywhere in this file, not in the Implementation Plan. Surfaced concretely once `build_agent_runtime_graph()` needed a real callable behind those nodes to be runnable at all. Draft ADR-0021 (`adr/0021-agent-runtime-llm-provider-interim.md`, Proposed) ships `placeholder_reason_fn` — an explicitly non-cognitive stand-in behind an injectable `reason_fn` interface — so the graph's orchestration could be built and tested without deciding this. Not yet decided by the user; names Anthropic Claude API, OpenAI API, and a locally-hosted open model as real options without picking one.

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

Still open: authority-check granularity for Security & Privacy (per task vs. per tool call) — surfaced during research but not asked yet, not blocking anything designed so far. Draft ADR-0020 (`adr/0020-security-authorize-interim-default.md`, Proposed) now proposes an interim fail-open, logged-but-not-enforced default for `DefaultBoundaryGate.authorize()` so implementation could proceed without deciding the real question — not yet decided by the user.

## Scaling to the remaining 10 components

Next, per Implementation Plan build-sequence order: User & Portfolio (01) — Phase 1 — then Tools & Environment (11), Data & Sources (02), Data Processing & Quality (03), Knowledge & Entity Model (04), Event & Observation (07), Analysis & Reasoning (08), Decision & Policy (12), Interaction & Notification (13), and the still-unresolved Learning & Evaluation (14). Mixed grounding continues: literature search first, engineering discussion if nothing substantive turns up.

## Implementation plan

Full build-sequence + technology plan published as an Artifact, "Implementation Plan." Covers all 18 components: concrete tech + build detail for Agent Runtime and Memory (the two with completed designs), and a dependency-ordered build sequence with design-readiness status for the other 16 — no technology invented for designs that don't exist yet.

## First real implementation round (2026-08-26)

- **Memory vendor resolved: Mem0** (ADR-0010 updated from "Accepted, partially" to "Accepted"). The per-decision fit check the ADR always called for (structural partition, quarantine-at-write, A-MEM-style linking, active working set) is deferred to component 06's own implementation round, not skipped.
- **Minimal Python scaffolding added**: `pyproject.toml` (Python >=3.11, pytest as a dev extra, `src` on the test pythonpath). No LangGraph or DB drivers yet — those arrive with Agent Runtime's and System Infrastructure's own rounds.
- **First real (non-stub) logic written**, three components in parallel, each following `loop.md` exactly:
  - **Reliability & Resilience (15)**: `DefaultFailureClassifier` (real heuristic — 3+ same-component failures in a row in `FailureEvent.history` classifies as `LOOP_OR_CASCADE`, else `TRANSIENT`) and `DefaultCircuitBreaker` (real per-tool tripped-state; `find_alternative` takes an optional injectable mapping rather than inventing the Tools & Environment interchangeability data it doesn't have). 8 tests.
  - **Observability & Governance (16)**: `DefaultAuditManager`, persisting real JSON-lines audit events to `audit.log`, mirroring the existing `trace.log` pattern. 1 test.
  - **Security & Privacy (17)**: `DefaultBoundaryGate` — `tag_provenance` (fully specified, always UNTRUSTED) and `authenticate` (minimal non-empty-string placeholder) implemented directly; `authorize` hit the genuine open gap (authority-check granularity) and correctly produced **[ADR-0020](adr/0020-security-authorize-interim-default.md)** (Status: Proposed) instead of silently deciding — ships a fail-open, logged-but-not-enforced interim default. 8 tests. **This is a real decision now waiting on the user, not a sequencing item.**
  - In every case the existing `Stub*` classes were left untouched (still the lightweight test doubles); the new `Default*` classes are additional adapters behind the same Protocol ports.
- **Verified together, not just individually**: full `pytest tests/` run after all three landed — 17 passed, no conflicts between the parallel agents' work.
- **Tooling note**: bare `python3`/`python3.11` on this machine has no pytest and is externally-managed (PEP 668); subagents used `uv` to create a project-local `.venv` and install pytest. `uv.lock` is committed for reproducibility; `.venv/` stays gitignored as before. This wasn't planned in `pyproject.toml` originally — flagged here rather than silently adopting a new tool without a note.
- **Next**: System Infrastructure (18) — needs a real local Postgres/Redis to implement against; plan is docker-compose for local dev, cloud credentials deliberately not requested this round. Agent Runtime (10) — needs the LangGraph dependency added and its fig. 1/2 loop actually built as a graph, the largest remaining piece. Memory (06) — Mem0 now picked, real implementation (and the deferred fit-check) still to do. The other 10 components remain whiteboard-only and need a design-framework pass before real code, unchanged from before.
