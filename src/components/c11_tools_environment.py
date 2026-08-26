"""Tools & Environment (component 11) — everything the agent can act
upon or interact with.

Whiteboard-level only (Component Whiteboards artifact, card 11) — no
low-level design or ADRs yet. Interface: <- Agent Runtime (10).
Reliability & Resilience's CircuitBreaker.find_alternative() (component
15) depends on this component exposing which tools are interchangeable
— not yet designed here.
"""

from dataclasses import dataclass

from cross_cutting.observability import traced


@dataclass
class Tool:
    name: str
    schema: dict


@dataclass
class ToolCall:
    tool_name: str
    arguments: dict


@dataclass
class ToolResult:
    call: ToolCall
    output: dict
    ok: bool


class ToolsEnvironment:
    def register_tool(self, tool: Tool) -> None:
        with traced("ToolsEnvironment.register_tool"):
            return None

    def discover_tool(self, need: str) -> list[Tool]:
        with traced("ToolsEnvironment.discover_tool"):
            return []

    def select_tool(self, need: str, candidates: list[Tool]) -> Tool:
        with traced("ToolsEnvironment.select_tool"):
            return Tool(name="stub", schema={})

    def execute_tool(self, call: ToolCall) -> ToolResult:
        with traced("ToolsEnvironment.execute_tool"):
            return ToolResult(call=ToolCall(tool_name="stub", arguments={}), output={}, ok=True)

    def validate_result(self, result: ToolResult) -> bool:
        with traced("ToolsEnvironment.validate_result"):
            return True

    def retry_tool(self, call: ToolCall) -> ToolResult:
        with traced("ToolsEnvironment.retry_tool"):
            return ToolResult(call=ToolCall(tool_name="stub", arguments={}), output={}, ok=True)

    def switch_tool(self, failed: Tool, need: str) -> Tool:
        with traced("ToolsEnvironment.switch_tool"):
            return Tool(name="stub", schema={})

    def interact_with_environment(self, action: dict) -> dict:
        with traced("ToolsEnvironment.interact_with_environment"):
            return {}
