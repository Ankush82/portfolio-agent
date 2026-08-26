"""Driver script for the Agent Runtime blueprint (component 10).

Runs one simulated trajectory end to end, in the order Agent Runtime
Design fig. 1 describes: a User & Portfolio trigger creates a task,
Planner breaks it into checkpoints, then for each checkpoint the
Executor runs Reason -> Act -> Observe (with Act calling out to Tools
& Environment, component 11), State Manager tracks state throughout,
and Workflow Manager finalizes the trajectory.

Not itself part of any component — this just calls the stubs in the
sequence the design specifies, so the wiring can be observed in
trace.log.
"""

from components.c01_user_portfolio import UserPortfolio
from components.c10_agent_runtime import (
    Checkpoint,
    Executor,
    Planner,
    StateManager,
    TaskManager,
    WorkflowManager,
)
from components.c11_tools_environment import ToolCall, ToolsEnvironment


def main() -> None:
    user_portfolio = UserPortfolio()
    user = user_portfolio.onboard_user(details={})
    snapshot = user_portfolio.synchronize_portfolio(portfolio=None)

    task_manager = TaskManager()
    task = task_manager.create_task(trigger={"snapshot": snapshot})

    planner = Planner()
    checkpoints = planner.plan_checkpoints(task=task)

    state_manager = StateManager()
    state_manager.update_state(task_id=task.id, delta={"status": "planned"})

    tools_environment = ToolsEnvironment()

    # The stub always returns an empty checkpoint list, so fall back to
    # one placeholder checkpoint here purely to keep the Executor loop
    # (and its trace) meaningful — this is not part of the design.
    for checkpoint in checkpoints or [Checkpoint(id="stub-checkpoint", subgoal={})]:
        executor = Executor()
        decision = executor.reason(checkpoint=checkpoint, context={})
        result = executor.act(decision=decision)
        # act() is meant to call Tools & Environment internally (c10's
        # own docstring); the stub can't do that yet, so the driver
        # makes that cross-component call here to keep it visible.
        tool_result = tools_environment.execute_tool(
            call=ToolCall(tool_name="stub", arguments=decision)
        )
        executor.observe(result={**result, "tool_result": tool_result})

    workflow_manager = WorkflowManager()
    workflow_manager.finalize(task=task, checkpoints=checkpoints)

    print(f"Trace complete for task {task.id} (user {user.id}). See trace.log.")


if __name__ == "__main__":
    main()
