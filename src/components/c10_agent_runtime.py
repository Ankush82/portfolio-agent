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


class TaskManager:
    def create_task(self, trigger: dict) -> Task:
        with traced("TaskManager.create_task"):
            return Task(id="stub-id", trigger={})

    def pause(self, task_id: str) -> None:
        with traced("TaskManager.pause"):
            return None

    def resume(self, task_id: str) -> None:
        with traced("TaskManager.resume"):
            return None

    def terminate(self, task_id: str) -> None:
        with traced("TaskManager.terminate"):
            return None


class Planner:
    def plan_checkpoints(self, task: Task) -> list[Checkpoint]:
        """Fig. 1: breaks a task into subgoals upfront (ADR-0001)."""
        with traced("Planner.plan_checkpoints"):
            return []


class Executor:
    """Fig. 2's Reason / Act / Observe loop, inside one checkpoint."""

    def reason(self, checkpoint: Checkpoint, context: dict) -> dict:
        with traced("Executor.reason"):
            return {}

    def act(self, decision: dict) -> dict:
        """Calls Tools & Environment (component 11), or
        DelegationManager.delegate() below. Either way, the result is
        provenance-tagged (BoundaryGate.tag_provenance) before
        observe() — ADR-0003 / ADR-0018."""
        with traced("Executor.act"):
            return {}

    def observe(self, result: dict) -> dict:
        """On failure: FailureClassifier.classify() first (ADR-0015),
        not an automatic replan. See RecoveryManager below."""
        with traced("Executor.observe"):
            return {}

    def assess_stakes(self, step: dict) -> bool:
        """True if this step needs step-level reflection now, rather
        than only at trajectory end (ADR-0002)."""
        with traced("Executor.assess_stakes"):
            return True

    def reflect_step(self, step: dict) -> None:
        with traced("Executor.reflect_step"):
            return None


class StateManager:
    """Tracks AgentState (Task · Plan · Execution · Step) continuously
    — drawn above the row in fig. 1, not as a step in the flow."""

    def get_state(self, task_id: str) -> dict:
        with traced("StateManager.get_state"):
            return {}

    def update_state(self, task_id: str, delta: dict) -> None:
        with traced("StateManager.update_state"):
            return None


class AgentCoordinator:
    def coordinate(self, task: Task) -> TrajectoryOutcome:
        """Runs fig. 1 end to end: Planner → per-checkpoint Executor
        loop (fig. 2) → WorkflowManager.finalize()."""
        with traced("AgentCoordinator.coordinate"):
            return TrajectoryOutcome(task_id="stub-id", result={})


class DelegationManager:
    def delegate(self, sub_task: dict) -> dict:
        """Fig. 2's delegation branch. Output is tagged UNTRUSTED like
        document content (ADR-0018), never trusted by default."""
        with traced("DelegationManager.delegate"):
            return {}


class RecoveryManager:
    def recover(self, checkpoint: Checkpoint, failure: dict) -> Checkpoint:
        """Replan-first, bounded retry budget (ADR-0004) — but only
        reached for transient failures; FailureClassifier routes
        loop/cascade patterns to CircuitBreaker instead (ADR-0015)."""
        with traced("RecoveryManager.recover"):
            return Checkpoint(id="stub-id", subgoal={})

    def escalate(self, checkpoint: Checkpoint, reason: str) -> None:
        """→ Decision & Policy (component 12), once the retry budget
        is exhausted."""
        with traced("RecoveryManager.escalate"):
            return None


class WorkflowManager:
    def finalize(self, task: Task, checkpoints: list[Checkpoint]) -> TrajectoryOutcome:
        """Assembles the trajectory once every checkpoint completes.
        Trajectory-level reflection always runs here, regardless of
        what happened at step level (ADR-0002, Reflexion pattern)."""
        with traced("WorkflowManager.finalize"):
            return TrajectoryOutcome(task_id="stub-id", result={})
