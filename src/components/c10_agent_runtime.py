"""Agent Runtime (component 10) — the control plane for agentic
behavior. Generic machinery; never touches domain data directly.

Design: Agent Runtime Design, fig. 1 (trajectory) and fig. 2 (inside
one checkpoint)
Decisions: ADR-0001 (hybrid checkpoint loop), ADR-0002 (stakes-
dependent reflection), ADR-0003 (in-runtime provenance tagging,
extended by ADR-0018), ADR-0004 (replan-first recovery — partially
superseded by ADR-0015, see the addendum on the design artifact)
Technology: LangGraph (ADR-0009)

The `Default*` classes below are the first real (non-stub) logic for
this component. `DefaultStateManager` is deliberately in-memory only
— System Infrastructure's real Postgres-backed store is being built
in parallel by a separate subagent and isn't ready yet; wiring to it
is out of scope for this pass, not forgotten.

`build_agent_runtime_graph()` is the real LangGraph implementation of
fig. 2 (ADR-0009). Its reason/assess-stakes nodes call an injected
`reason_fn` rather than a hardcoded model call. The LLM-provider gap
ADR-0021 named (status: superseded) is now resolved by ADR-0043
(`adr/0043-llm-provider-resolved-openrouter.md`): `get_reason_fn()`
(`src/llm.py`) returns a real OpenRouter-backed `reason_fn` when
`OPENROUTER_API_KEY` is set, and `placeholder_reason_fn` below
otherwise — the graph's own shape never had to change for that swap,
exactly as ADR-0021's Consequences predicted it wouldn't.
`placeholder_reason_fn` stays as the explicitly non-cognitive stand-in
used whenever no key is configured, or whenever a caller/test injects
it directly for deterministic, no-network behavior.
"""

import uuid
from dataclasses import dataclass, field
from typing import Callable, Protocol, TypedDict

from langgraph.graph import END, StateGraph

from cross_cutting.observability import AuditManager, DefaultAuditManager, traced
from cross_cutting.reliability import (
    CircuitBreaker,
    DefaultCircuitBreaker,
    DefaultFailureClassifier,
    FailureClassifier,
    FailureEvent,
    FailureType,
)
from cross_cutting.security import BoundaryGate, DefaultBoundaryGate
from llm import get_reason_fn


@dataclass
class Task:
    id: str
    trigger: dict


@dataclass
class Checkpoint:
    id: str
    subgoal: dict


@dataclass
class TrajectoryOutcome:
    task_id: str
    result: dict
    reflection: dict | None = None


class TaskManager(Protocol):
    def create_task(self, trigger: dict) -> Task:
        ...

    def pause(self, task_id: str) -> None:
        ...

    def resume(self, task_id: str) -> None:
        ...

    def terminate(self, task_id: str) -> None:
        ...


class StubTaskManager:
    """Structural implementation of TaskManager. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def create_task(self, trigger: dict) -> Task:
        with traced("StubTaskManager.create_task"):
            return Task(id="stub-id", trigger={})

    def pause(self, task_id: str) -> None:
        with traced("StubTaskManager.pause"):
            return None

    def resume(self, task_id: str) -> None:
        with traced("StubTaskManager.resume"):
            return None

    def terminate(self, task_id: str) -> None:
        with traced("StubTaskManager.terminate"):
            return None


class DefaultTaskManager:
    """Real implementation of TaskManager: real task lifecycle backed by
    an instance-level dict keyed by task id. `pause`/`resume`/`terminate`
    mutate that state for real rather than silently no-opping, and raise
    `KeyError` for an unknown task id rather than pretending the call
    succeeded."""

    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}

    def _existing(self, task_id: str) -> dict:
        if task_id not in self._tasks:
            raise KeyError(f"DefaultTaskManager: unknown task id {task_id!r}")
        return self._tasks[task_id]

    def create_task(self, trigger: dict) -> Task:
        with traced("DefaultTaskManager.create_task"):
            task = Task(id=str(uuid.uuid4()), trigger=trigger)
            self._tasks[task.id] = {"task": task, "status": "created"}
            return task

    def pause(self, task_id: str) -> None:
        with traced("DefaultTaskManager.pause"):
            self._existing(task_id)["status"] = "paused"

    def resume(self, task_id: str) -> None:
        with traced("DefaultTaskManager.resume"):
            self._existing(task_id)["status"] = "running"

    def terminate(self, task_id: str) -> None:
        with traced("DefaultTaskManager.terminate"):
            self._existing(task_id)["status"] = "terminated"

    def status(self, task_id: str) -> str:
        """Not part of the TaskManager Protocol — a small real-behavior
        accessor so callers (and tests) can observe the lifecycle state
        this class actually tracks, rather than only its side effects."""
        with traced("DefaultTaskManager.status"):
            return self._existing(task_id)["status"]


class Planner(Protocol):
    def plan_checkpoints(self, task: Task) -> list[Checkpoint]:
        """Fig. 1: breaks a task into subgoals upfront (ADR-0001)."""
        ...


class StubPlanner:
    """Structural implementation of Planner. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def plan_checkpoints(self, task: Task) -> list[Checkpoint]:
        with traced("StubPlanner.plan_checkpoints"):
            return []


class Executor(Protocol):
    """Fig. 2's Reason / Act / Observe loop, inside one checkpoint."""

    def reason(self, checkpoint: Checkpoint, context: dict) -> dict:
        ...

    def act(self, decision: dict) -> dict:
        """Calls Tools & Environment (component 11), or
        DelegationManager.delegate() below. Either way, the result is
        provenance-tagged (BoundaryGate.tag_provenance) before
        observe() — ADR-0003 / ADR-0018."""
        ...

    def observe(self, result: dict) -> dict:
        """On failure: FailureClassifier.classify() first (ADR-0015),
        not an automatic replan. See RecoveryManager below."""
        ...

    def assess_stakes(self, step: dict) -> bool:
        """True if this step needs step-level reflection now, rather
        than only at trajectory end (ADR-0002)."""
        ...

    def reflect_step(self, step: dict) -> None:
        ...


class StubExecutor:
    """Structural implementation of Executor. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def reason(self, checkpoint: Checkpoint, context: dict) -> dict:
        with traced("StubExecutor.reason"):
            return {}

    def act(self, decision: dict) -> dict:
        with traced("StubExecutor.act"):
            return {}

    def observe(self, result: dict) -> dict:
        with traced("StubExecutor.observe"):
            return {}

    def assess_stakes(self, step: dict) -> bool:
        with traced("StubExecutor.assess_stakes"):
            return True

    def reflect_step(self, step: dict) -> None:
        with traced("StubExecutor.reflect_step"):
            return None


class StateManager(Protocol):
    """Tracks AgentState (Task · Plan · Execution · Step) continuously
    — drawn above the row in fig. 1, not as a step in the flow."""

    def get_state(self, task_id: str) -> dict:
        ...

    def update_state(self, task_id: str, delta: dict) -> None:
        ...


class StubStateManager:
    """Structural implementation of StateManager. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def get_state(self, task_id: str) -> dict:
        with traced("StubStateManager.get_state"):
            return {}

    def update_state(self, task_id: str, delta: dict) -> None:
        with traced("StubStateManager.update_state"):
            return None


class DefaultStateManager:
    """Real implementation of StateManager. In-memory only for this pass
    — System Infrastructure's real Postgres-backed store is being built
    in parallel by a separate subagent and isn't ready yet; this is the
    honest, correctly-scoped default until that lands, not a permanent
    choice."""

    def __init__(self) -> None:
        self._state: dict[str, dict] = {}

    def get_state(self, task_id: str) -> dict:
        with traced("DefaultStateManager.get_state"):
            return dict(self._state.get(task_id, {}))

    def update_state(self, task_id: str, delta: dict) -> None:
        with traced("DefaultStateManager.update_state"):
            self._state.setdefault(task_id, {}).update(delta)


class AgentCoordinator(Protocol):
    def coordinate(self, task: Task) -> TrajectoryOutcome:
        """Runs fig. 1 end to end: Planner → per-checkpoint Executor
        loop (fig. 2) → WorkflowManager.finalize()."""
        ...


class StubAgentCoordinator:
    """Structural implementation of AgentCoordinator. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def coordinate(self, task: Task) -> TrajectoryOutcome:
        with traced("StubAgentCoordinator.coordinate"):
            return TrajectoryOutcome(task_id="stub-id", result={})


class DefaultAgentCoordinator:
    """Real implementation of AgentCoordinator (fig. 1): Planner ->
    per-checkpoint run of the compiled LangGraph graph (fig. 2, built by
    `build_agent_runtime_graph` below) -> WorkflowManager.finalize().

    The compiled graph and its `reason_fn` are built once, outside this
    class, and injected here — this class only sequences fig. 1, it does
    not know or care what `reason_fn` is behind the graph."""

    def __init__(self, planner: Planner, compiled_graph, workflow_manager: "WorkflowManager") -> None:
        self._planner = planner
        self._compiled_graph = compiled_graph
        self._workflow_manager = workflow_manager

    def coordinate(self, task: Task) -> TrajectoryOutcome:
        with traced("DefaultAgentCoordinator.coordinate"):
            checkpoints = self._planner.plan_checkpoints(task)
            completed_checkpoints = []
            for checkpoint in checkpoints:
                final_state = self._compiled_graph.invoke(initial_loop_state(checkpoint))
                completed_checkpoints.append(final_state["checkpoint"])
            return self._workflow_manager.finalize(task, completed_checkpoints)


class DelegationManager(Protocol):
    def delegate(self, sub_task: dict) -> dict:
        """Fig. 2's delegation branch. Output is tagged UNTRUSTED like
        document content (ADR-0018), never trusted by default."""
        ...


class StubDelegationManager:
    """Structural implementation of DelegationManager. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def delegate(self, sub_task: dict) -> dict:
        with traced("StubDelegationManager.delegate"):
            return {}


class DefaultDelegationManager:
    """Real implementation of DelegationManager. There is no real
    sub-agent to call yet, so the sub-agent's raw output is simulated as
    whatever dict `sub_task` already is — the point of this class is not
    the (nonexistent) delegation call itself, it's that whatever comes
    back gets tagged UNTRUSTED via BoundaryGate.tag_provenance before
    returning, exactly like document content (ADR-0003, ADR-0018)."""

    def __init__(self, boundary_gate: BoundaryGate | None = None) -> None:
        self._boundary_gate = boundary_gate or DefaultBoundaryGate()

    def delegate(self, sub_task: dict) -> dict:
        with traced("DefaultDelegationManager.delegate"):
            simulated_sub_agent_output = sub_task
            return self._boundary_gate.tag_provenance(simulated_sub_agent_output, source="peer_agent")


class RecoveryManager(Protocol):
    def recover(self, checkpoint: Checkpoint, failure: dict) -> Checkpoint:
        """Replan-first, bounded retry budget (ADR-0004) — but only
        reached for transient failures; FailureClassifier routes
        loop/cascade patterns to CircuitBreaker instead (ADR-0015)."""
        ...

    def escalate(self, checkpoint: Checkpoint, reason: str) -> None:
        """→ Decision & Policy (component 12), once the retry budget
        is exhausted."""
        ...


class StubRecoveryManager:
    """Structural implementation of RecoveryManager. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def recover(self, checkpoint: Checkpoint, failure: dict) -> Checkpoint:
        with traced("StubRecoveryManager.recover"):
            return Checkpoint(id="stub-id", subgoal={})

    def escalate(self, checkpoint: Checkpoint, reason: str) -> None:
        with traced("StubRecoveryManager.escalate"):
            return None


class EscalationRequired(Exception):
    """Raised by DefaultRecoveryManager.recover() when a failure cannot
    be routed to another autonomous replan — either FailureType.
    LOOP_OR_CASCADE (ADR-0015 routes these to the circuit breaker, not
    replan) or a TRANSIENT failure whose retry budget (ADR-0004) is
    already exhausted. `escalate()` has already logged the event before
    this is raised. Deliberately not a new outcome dataclass: recover()
    still returns a plain `Checkpoint` on the success path, per the
    task's own instruction not to invent an outcome type beyond what's
    needed; this exception is the minimal signal for the one case that
    genuinely isn't a Checkpoint."""

    def __init__(self, checkpoint: Checkpoint, reason: str) -> None:
        super().__init__(reason)
        self.checkpoint = checkpoint
        self.reason = reason


class DefaultRecoveryManager:
    """Real implementation of RecoveryManager (ADR-0004, revised by
    ADR-0015). Every failure is classified first via FailureClassifier;
    only FailureType.TRANSIENT is eligible for autonomous replan, and
    only while retry_budget hasn't been exhausted for that checkpoint.
    FailureType.LOOP_OR_CASCADE trips the named tool's circuit breaker,
    then — per ADR-0025, fig 15.1's own 'alternative exists?' branch,
    completed now that Tools & Environment's DefaultToolsEnvironment
    .alternatives_for() gives CircuitBreaker.find_alternative() a real
    mapping to consult — checks for an alternative tool. If one is
    available, this continues the checkpoint on that alternative
    instead of escalating; escalation is reserved for when no
    alternative exists, exactly as fig 15.1 diagrams it, not for every
    loop/cascade unconditionally as before ADR-0025.

    Real replanning *content* (what to actually try differently) needs
    the same reasoning capability this whole task is being honest about
    not having yet (see ADR-0021) — this replans by retrying the same
    checkpoint unchanged, which is the honest scope for orchestration
    logic alone: it advances the retry bookkeeping for real, it does not
    invent a smarter plan.
    """

    def __init__(
        self,
        retry_budget: int = 3,
        failure_classifier: FailureClassifier | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        audit_manager: AuditManager | None = None,
    ) -> None:
        self._retry_budget = retry_budget
        self._failure_classifier = failure_classifier or DefaultFailureClassifier()
        self._circuit_breaker = circuit_breaker or DefaultCircuitBreaker()
        self._audit_manager = audit_manager or DefaultAuditManager()
        self._retries_used: dict[str, int] = {}
        self._failure_history: dict[str, list[FailureEvent]] = {}

    def recover(self, checkpoint: Checkpoint, failure: dict) -> Checkpoint:
        with traced("DefaultRecoveryManager.recover"):
            history = self._failure_history.setdefault(checkpoint.id, [])
            event = FailureEvent(
                component=failure.get("component", "unknown"),
                tool=failure.get("tool"),
                error=failure.get("error", ""),
                history=list(history),
            )
            failure_type = self._failure_classifier.classify(event)
            history.append(event)

            if failure_type == FailureType.LOOP_OR_CASCADE:
                if event.tool:
                    self._circuit_breaker.trip(event.tool)
                    alternative = self._circuit_breaker.find_alternative(event.tool)
                    if alternative is not None:
                        self._audit_manager.record(
                            "tool_switch_on_loop_or_cascade",
                            {
                                "checkpoint_id": checkpoint.id,
                                "failed_tool": event.tool,
                                "alternative_tool": alternative,
                            },
                        )
                        return Checkpoint(
                            id=checkpoint.id,
                            subgoal={**checkpoint.subgoal, "preferred_tool": alternative},
                        )
                self.escalate(checkpoint, f"loop_or_cascade: {event.error}")
                raise EscalationRequired(checkpoint, "loop_or_cascade")

            used = self._retries_used.get(checkpoint.id, 0)
            if used >= self._retry_budget:
                self.escalate(checkpoint, f"retry_budget_exhausted: {event.error}")
                raise EscalationRequired(checkpoint, "retry_budget_exhausted")

            self._retries_used[checkpoint.id] = used + 1
            return checkpoint

    def escalate(self, checkpoint: Checkpoint, reason: str) -> None:
        with traced("DefaultRecoveryManager.escalate"):
            self._audit_manager.record(
                "escalation",
                {"checkpoint_id": checkpoint.id, "reason": reason},
            )


class WorkflowManager(Protocol):
    def finalize(self, task: Task, checkpoints: list[Checkpoint]) -> TrajectoryOutcome:
        """Assembles the trajectory once every checkpoint completes.
        Trajectory-level reflection always runs here, regardless of
        what happened at step level (ADR-0002, Reflexion pattern)."""
        ...


class StubWorkflowManager:
    """Structural implementation of WorkflowManager. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def finalize(self, task: Task, checkpoints: list[Checkpoint]) -> TrajectoryOutcome:
        with traced("StubWorkflowManager.finalize"):
            return TrajectoryOutcome(task_id="stub-id", result={})


class DefaultWorkflowManager:
    """Real implementation of WorkflowManager. Assembles a
    TrajectoryOutcome from the task and the checkpoints actually passed
    in — not a hardcoded stub value — and always runs trajectory-level
    reflection before returning (ADR-0002, Reflexion's own pattern,
    unconditional regardless of what happened at step level)."""

    def __init__(self, audit_manager: AuditManager | None = None) -> None:
        self._audit_manager = audit_manager or DefaultAuditManager()

    def finalize(self, task: Task, checkpoints: list[Checkpoint]) -> TrajectoryOutcome:
        with traced("DefaultWorkflowManager.finalize"):
            outcome = TrajectoryOutcome(
                task_id=task.id,
                result={
                    "checkpoint_ids": [checkpoint.id for checkpoint in checkpoints],
                    "checkpoint_count": len(checkpoints),
                },
            )
            return self.reflect_trajectory(outcome, checkpoints)

    def reflect_trajectory(
        self, outcome: TrajectoryOutcome, checkpoints: list[Checkpoint]
    ) -> TrajectoryOutcome:
        """Trajectory-level reflection (ADR-0002). Real reflection
        *content* needs the same reasoning capability this task is being
        honest about not having yet (see ADR-0021) — this records that
        the reflection step ran, via DefaultAuditManager, rather than
        fabricating an insight nothing actually reasoned about."""
        with traced("DefaultWorkflowManager.reflect_trajectory"):
            reflection = {
                "note": "placeholder trajectory reflection — no reasoning backend yet, see ADR-0021",
                "checkpoint_count": len(checkpoints),
            }
            self._audit_manager.record(
                "trajectory_reflection",
                {"task_id": outcome.task_id, **reflection},
            )
            outcome.reflection = reflection
            return outcome


class LoopState(TypedDict):
    """State schema for the compiled LangGraph graph below — fig. 2,
    inside one checkpoint. Carries the checkpoint itself, the running
    reason/act/observe(/assess_stakes/reflect/recover) history, the
    retry count (how many times DefaultRecoveryManager has replanned
    this checkpoint), and the done flag fig. 2's loop terminates on."""

    checkpoint: Checkpoint
    history: list[dict]
    retry_count: int
    done: bool
    pending_action: dict
    last_result: dict | None
    failure: dict | None
    checkpoint_complete: bool
    stakes_high: bool
    reflection: dict | None


def initial_loop_state(checkpoint: Checkpoint) -> LoopState:
    """The state a checkpoint's graph run starts from."""
    return LoopState(
        checkpoint=checkpoint,
        history=[],
        retry_count=0,
        done=False,
        pending_action={},
        last_result=None,
        failure=None,
        checkpoint_complete=False,
        stakes_high=False,
        reflection=None,
    )


def placeholder_reason_fn(request: dict) -> dict:
    """Explicitly NOT real cognition — see ADR-0021
    (`adr/0021-agent-runtime-llm-provider-interim.md`, status Proposed).
    No LLM provider has been chosen anywhere in this project, for this
    or any other component. This exists purely so
    `build_agent_runtime_graph`'s structure is buildable, runnable, and
    testable end to end without one.

    It does not look at `request["checkpoint"]`'s subgoal, the observed
    result, or anything else — it deterministically decides "checkpoint
    complete" after exactly one reason/act/observe cycle (the reason
    node's very first, and only, call always reports the checkpoint as
    complete once this action runs), and it always reports low stakes,
    so the stakes-dependent reflection branch (ADR-0002) is exercised as
    "skip reflection" every time. Real reasoning and real stakes
    assessment both require a real `reason_fn` implementation behind
    this same interface."""
    phase = request.get("phase")
    if phase == "reason":
        return {"action": {"tool": "noop"}, "checkpoint_complete": True}
    if phase == "assess_stakes":
        return {"stakes_high": False}
    raise ValueError(f"placeholder_reason_fn: unknown phase {phase!r}")


def build_agent_runtime_graph(
    recovery_manager: RecoveryManager,
    delegation_manager: DelegationManager,
    reason_fn: Callable[[dict], dict] | None = None,
    boundary_gate: BoundaryGate | None = None,
    audit_manager: AuditManager | None = None,
):
    """Builds and compiles the real LangGraph StateGraph for fig. 2 —
    the Reason / Act / Observe loop inside one checkpoint (ADR-0001,
    ADR-0009) — including the stakes-dependent reflection branch
    (ADR-0002) and the failure-classified recovery branch (ADR-0015).

    Graph shape:
      reason -> act -> observe
        -> [failure?]  -> recover -> [escalated? -> finish : -> reason]
        -> [no failure] -> assess_stakes
             -> [stakes_high?] -> reflect -> [subgoal met? -> finish : -> reason]
             -> [not stakes_high]          -> [subgoal met? -> finish : -> reason]

    `reason` and `assess_stakes` are the two nodes that call `reason_fn`
    — see this module's docstring and ADR-0021/ADR-0043 for why that's
    an injected callable rather than a hardcoded model call. Leaving
    `reason_fn` unspecified (`None`) resolves it via `get_reason_fn`
    (`src/llm.py`) at call time — the real OpenRouter-backed function
    when `OPENROUTER_API_KEY` is set, `placeholder_reason_fn`
    otherwise — the same `Default*`-vs-`Stub*` selection pattern
    `DefaultInfrastructure`/`StubInfrastructure` already establishes.
    Passing `reason_fn` explicitly (as every existing test in
    `tests/components/test_agent_runtime.py` does with
    `placeholder_reason_fn`) always wins, regardless of environment.
    """
    boundary_gate = boundary_gate or DefaultBoundaryGate()
    audit_manager = audit_manager or DefaultAuditManager()
    reason_fn = reason_fn or get_reason_fn(placeholder_reason_fn, audit_manager=audit_manager)

    def reason_node(state: LoopState) -> dict:
        with traced("agent_runtime_graph.reason"):
            request = {
                "phase": "reason",
                "checkpoint": state["checkpoint"],
                "history": state["history"],
                "retry_count": state["retry_count"],
            }
            output = reason_fn(request)
            return {
                "history": state["history"] + [{"phase": "reason", "output": output}],
                "pending_action": output.get("action", {}),
                "checkpoint_complete": bool(output.get("checkpoint_complete", False)),
            }

    def act_node(state: LoopState) -> dict:
        with traced("agent_runtime_graph.act"):
            action = state.get("pending_action", {})
            # No real Tools & Environment (component 11) integration
            # exists yet. Either delegate (ADR-0018 tagging, via
            # DelegationManager) or produce a provenance-tagged
            # simulated tool result — either way fig. 2's Act -> Observe
            # edge requires the result be tagged UNTRUSTED before
            # Observe can see it (ADR-0003).
            if action.get("delegate"):
                raw_result = delegation_manager.delegate(action["delegate"])
            else:
                raw_result = boundary_gate.tag_provenance(
                    {"action": action, "output": None}, source="tool_call"
                )
            return {
                "history": state["history"] + [{"phase": "act", "output": raw_result}],
                "last_result": raw_result,
            }

    def observe_node(state: LoopState) -> dict:
        with traced("agent_runtime_graph.observe"):
            result = state.get("last_result") or {}
            error = result.get("error")
            failure_event = (
                {"component": "executor", "tool": result.get("action", {}).get("tool"), "error": error}
                if error
                else None
            )
            return {
                "history": state["history"] + [{"phase": "observe", "output": result}],
                "failure": failure_event,
            }

    def assess_stakes_node(state: LoopState) -> dict:
        with traced("agent_runtime_graph.assess_stakes"):
            request = {
                "phase": "assess_stakes",
                "checkpoint": state["checkpoint"],
                "last_result": state.get("last_result"),
            }
            output = reason_fn(request)
            return {
                "history": state["history"] + [{"phase": "assess_stakes", "output": output}],
                "stakes_high": bool(output.get("stakes_high", False)),
            }

    def reflect_node(state: LoopState) -> dict:
        with traced("agent_runtime_graph.reflect_step"):
            reflection = {
                "note": "placeholder step-level reflection — no reasoning backend yet, see ADR-0021",
                "checkpoint_id": state["checkpoint"].id,
            }
            audit_manager.record(
                "step_reflection", {"checkpoint_id": state["checkpoint"].id, **reflection}
            )
            return {
                "history": state["history"] + [{"phase": "reflect", "output": reflection}],
                "reflection": reflection,
            }

    def recover_node(state: LoopState) -> dict:
        with traced("agent_runtime_graph.recover"):
            failure = state.get("failure") or {}
            try:
                replanned_checkpoint = recovery_manager.recover(state["checkpoint"], failure)
                return {
                    "checkpoint": replanned_checkpoint,
                    "retry_count": state["retry_count"] + 1,
                    "failure": None,
                    "history": state["history"] + [{"phase": "recover", "output": "replanned"}],
                }
            except EscalationRequired as exc:
                return {
                    "done": True,
                    "failure": {"escalated": True, "reason": exc.reason},
                    "history": state["history"]
                    + [{"phase": "recover", "output": f"escalated: {exc.reason}"}],
                }

    def finish_node(state: LoopState) -> dict:
        with traced("agent_runtime_graph.finish"):
            return {"done": True}

    def route_after_observe(state: LoopState) -> str:
        return "recover" if state.get("failure") else "assess_stakes"

    def subgoal_route(state: LoopState) -> str:
        return "finish" if state.get("checkpoint_complete") else "reason"

    def route_after_assess_stakes(state: LoopState) -> str:
        return "reflect" if state.get("stakes_high") else subgoal_route(state)

    def route_after_recover(state: LoopState) -> str:
        return "finish" if state.get("done") else "reason"

    graph = StateGraph(LoopState)
    graph.add_node("reason", reason_node)
    graph.add_node("act", act_node)
    graph.add_node("observe", observe_node)
    graph.add_node("assess_stakes", assess_stakes_node)
    graph.add_node("reflect", reflect_node)
    graph.add_node("recover", recover_node)
    graph.add_node("finish", finish_node)

    graph.set_entry_point("reason")
    graph.add_edge("reason", "act")
    graph.add_edge("act", "observe")
    graph.add_conditional_edges(
        "observe", route_after_observe, {"recover": "recover", "assess_stakes": "assess_stakes"}
    )
    graph.add_conditional_edges(
        "assess_stakes",
        route_after_assess_stakes,
        {"reflect": "reflect", "reason": "reason", "finish": "finish"},
    )
    graph.add_conditional_edges(
        "reflect", subgoal_route, {"reason": "reason", "finish": "finish"}
    )
    graph.add_conditional_edges(
        "recover", route_after_recover, {"reason": "reason", "finish": "finish"}
    )
    graph.add_edge("finish", END)

    return graph.compile()
