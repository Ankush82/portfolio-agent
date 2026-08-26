"""Agent Runtime (component 10) — the control plane for agentic
behavior. Generic machinery; never touches domain data directly.

Design: Agent Runtime Design, fig. 1 (trajectory) and fig. 2 (inside
one checkpoint)
Decisions: ADR-0001 (hybrid checkpoint loop), ADR-0002 (stakes-
dependent reflection), ADR-0003 (in-runtime provenance tagging,
extended by ADR-0018), ADR-0004 (replan-first recovery — partially
superseded by ADR-0015, see the addendum on the design artifact)
Technology: LangGraph (ADR-0009)
"""

from dataclasses import dataclass, field
from typing import Protocol

from cross_cutting.observability import traced
from cross_cutting.reliability import CircuitBreaker, FailureClassifier
from cross_cutting.security import BoundaryGate


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
