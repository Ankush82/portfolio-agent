import pytest

from components.c10_agent_runtime import (
    Checkpoint,
    DefaultDelegationManager,
    DefaultRecoveryManager,
    DefaultTaskManager,
    EscalationRequired,
    build_agent_runtime_graph,
    placeholder_reason_fn,
)
from cross_cutting.reliability import DefaultCircuitBreaker, DefaultFailureClassifier
from cross_cutting.security import Provenance


# --- DefaultTaskManager -----------------------------------------------


def test_create_task_generates_a_real_uuid_and_stores_created_status():
    manager = DefaultTaskManager()

    task = manager.create_task({"trigger": "new filing"})

    assert task.id  # a real id was generated
    assert len(task.id) == 36  # uuid4 string form
    assert manager.status(task.id) == "created"


def test_create_task_generates_distinct_ids_for_distinct_tasks():
    manager = DefaultTaskManager()

    first = manager.create_task({})
    second = manager.create_task({})

    assert first.id != second.id


def test_pause_resume_terminate_mutate_status_for_real():
    manager = DefaultTaskManager()
    task = manager.create_task({})

    manager.pause(task.id)
    assert manager.status(task.id) == "paused"

    manager.resume(task.id)
    assert manager.status(task.id) == "running"

    manager.terminate(task.id)
    assert manager.status(task.id) == "terminated"


def test_pause_unknown_task_id_raises_key_error_not_silent_noop():
    manager = DefaultTaskManager()

    with pytest.raises(KeyError):
        manager.pause("no-such-task")


def test_resume_unknown_task_id_raises_key_error():
    manager = DefaultTaskManager()

    with pytest.raises(KeyError):
        manager.resume("no-such-task")


def test_terminate_unknown_task_id_raises_key_error():
    manager = DefaultTaskManager()

    with pytest.raises(KeyError):
        manager.terminate("no-such-task")


# --- DefaultRecoveryManager --------------------------------------------


def _transient_failure(tool: str = "web_search") -> dict:
    return {"component": "executor", "tool": tool, "error": "timeout"}


def test_transient_failure_returns_replanned_checkpoint_and_decrements_budget():
    manager = DefaultRecoveryManager(retry_budget=3)
    checkpoint = Checkpoint(id="cp-1", subgoal={})

    result = manager.recover(checkpoint, _transient_failure())

    assert isinstance(result, Checkpoint)
    assert result.id == checkpoint.id
    # One retry used out of a budget of 3 — two replans still available
    # before this checkpoint would be forced to escalate.
    assert manager._retries_used[checkpoint.id] == 1


def test_transient_failure_escalates_once_retry_budget_is_exhausted():
    manager = DefaultRecoveryManager(retry_budget=1)
    checkpoint = Checkpoint(id="cp-2", subgoal={})

    # First failure: budget not yet exhausted, replans.
    manager.recover(checkpoint, _transient_failure())

    # Second failure on the same checkpoint: budget (1) is now used up.
    with pytest.raises(EscalationRequired) as excinfo:
        manager.recover(checkpoint, _transient_failure())

    assert excinfo.value.reason == "retry_budget_exhausted"
    assert excinfo.value.checkpoint is checkpoint


def test_three_in_a_row_same_component_failures_classify_as_loop_or_cascade_and_escalate():
    """Uses DefaultFailureClassifier's real 3-in-a-row rule directly,
    not a reimplementation of it: the first three calls build up
    same-component history (each individually still TRANSIENT, since
    classify() only looks at *prior* history, which is shorter than the
    3-entry window on each of those calls); the fourth call's prior
    history is exactly 3 same-component entries, which is what
    DefaultFailureClassifier.classify() treats as LOOP_OR_CASCADE."""
    circuit_breaker = DefaultCircuitBreaker()
    manager = DefaultRecoveryManager(
        retry_budget=10,
        failure_classifier=DefaultFailureClassifier(),
        circuit_breaker=circuit_breaker,
    )
    checkpoint = Checkpoint(id="cp-3", subgoal={})

    for _ in range(3):
        result = manager.recover(checkpoint, _transient_failure(tool="flaky_tool"))
        assert isinstance(result, Checkpoint)

    with pytest.raises(EscalationRequired) as excinfo:
        manager.recover(checkpoint, _transient_failure(tool="flaky_tool"))

    assert excinfo.value.reason == "loop_or_cascade"
    assert circuit_breaker.is_available("flaky_tool") is False


def test_escalate_records_an_audit_event(tmp_path, monkeypatch):
    from cross_cutting import observability

    audit_log_path = tmp_path / "audit.log"
    monkeypatch.setattr(observability, "AUDIT_LOG_PATH", audit_log_path)

    manager = DefaultRecoveryManager(audit_manager=observability.DefaultAuditManager())
    checkpoint = Checkpoint(id="cp-4", subgoal={})

    manager.escalate(checkpoint, "some reason")

    lines = audit_log_path.read_text().splitlines()
    assert len(lines) == 1
    assert '"event_type": "escalation"' in lines[0]


# --- DefaultDelegationManager --------------------------------------------


def test_delegate_tags_returned_output_untrusted():
    manager = DefaultDelegationManager()

    result = manager.delegate({"report": "sub-agent findings"})

    assert result["provenance"] == Provenance.UNTRUSTED.name
    assert result["report"] == "sub-agent findings"


# --- The compiled LangGraph graph ----------------------------------------


def test_compiled_graph_completes_one_full_reason_act_observe_cycle():
    graph = build_agent_runtime_graph(
        recovery_manager=DefaultRecoveryManager(),
        delegation_manager=DefaultDelegationManager(),
        reason_fn=placeholder_reason_fn,
    )
    checkpoint = Checkpoint(id="cp-graph-1", subgoal={"goal": "assess portfolio risk"})

    from components.c10_agent_runtime import initial_loop_state

    final_state = graph.invoke(initial_loop_state(checkpoint))

    # It actually completed, not just "didn't crash".
    assert final_state["done"] is True
    assert final_state["checkpoint_complete"] is True
    assert final_state["failure"] is None

    # placeholder_reason_fn always reports low stakes, so the reflection
    # branch (ADR-0002) was correctly skipped, not silently missing.
    assert final_state["stakes_high"] is False
    assert final_state["reflection"] is None

    # No failure occurred, so DefaultRecoveryManager.recover() was never
    # invoked — retry_count stays at its initial value.
    assert final_state["retry_count"] == 0

    # Exactly one reason/act/observe/assess_stakes cycle happened, in
    # order — not zero, not more than one.
    phases = [entry["phase"] for entry in final_state["history"]]
    assert phases == ["reason", "act", "observe", "assess_stakes"]

    # The checkpoint carried through the graph is the same one it started
    # with (no failure meant no replan).
    assert final_state["checkpoint"].id == checkpoint.id


def test_compiled_graph_act_result_is_provenance_tagged_before_observe():
    graph = build_agent_runtime_graph(
        recovery_manager=DefaultRecoveryManager(),
        delegation_manager=DefaultDelegationManager(),
        reason_fn=placeholder_reason_fn,
    )
    checkpoint = Checkpoint(id="cp-graph-2", subgoal={})

    from components.c10_agent_runtime import initial_loop_state

    final_state = graph.invoke(initial_loop_state(checkpoint))

    act_entry = next(entry for entry in final_state["history"] if entry["phase"] == "act")
    assert act_entry["output"]["provenance"] == Provenance.UNTRUSTED.name
