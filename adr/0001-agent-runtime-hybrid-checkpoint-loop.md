# 0001 — Reason/act loop: hybrid with checkpoints

**Status:** Accepted — 2026-08-25
**Component:** Agent Runtime (10)

## Context

The literature review (`portfolio_ai_three_literature_reviews.md`, section 2) surfaces two competing shapes for how an agent moves from a task to a trajectory:

- **AgentBench** frames evaluation around Task → Trajectory → Environment interaction → Outcome, which assumes a plan that can later be checked against.
- **ReAct** interleaves reasoning and acting one step at a time — reason, act, observe, reason again — so the agent adapts to what it just observed, at the cost of the trajectory itself being dynamic and harder to evaluate as "one correct answer."

Agent Runtime's own sub-components (Task Manager, Planner, Executor, State Manager) need to commit to one shape, since Planner and Executor's division of responsibility depends on it, and the system continuously ingests new financial information mid-trajectory.

## Decision

Hybrid with checkpoints: Planner sets subgoals (checkpoints) upfront; ReAct-style reasoning and acting interleaves within each checkpoint.

## Alternatives considered

- **Plan-then-execute.** Planner produces the full plan before Executor runs any of it. Easier to trace and evaluate as one unit, but can't adapt mid-trajectory to what it just observed. Rejected as too rigid for a system whose inputs change while it's still reasoning.
- **Fully interleaved (ReAct).** Reason → act → observe → reason, one step at a time, no upfront plan at all. Adapts fully, but the trajectory itself is dynamic, which makes "did it do the right thing" hard to evaluate, and gives a bad reasoning step nothing to bound how far it can drift. Rejected in favor of the hybrid for that reason.

## Consequences

- Bounds how far a bad reasoning step can drift before anything checks it — checkpoint boundaries double as evaluation boundaries.
- Adds a real sub-component boundary between Planner (checkpoint-level) and Executor (step-level within a checkpoint) that has to be designed and kept consistent.
- Evaluation (component 14, not yet designed) will need to reason about both checkpoint-level and step-level granularity, not just one.

## Related

- Full trajectory design: [Agent Runtime Design](https://claude.ai/code/artifact/89de3618-d0b8-44f3-af85-73dbfbd73df6), fig. 1 and fig. 2.
- Logged narratively in `../checkpoint.md`.
