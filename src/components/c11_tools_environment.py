"""Tools & Environment (component 11) — everything the agent can act
upon or interact with.

Whiteboard-level only (Component Whiteboards artifact, card 11) — no
low-level design or ADRs yet. Interface: <- Agent Runtime (10).
Reliability & Resilience's CircuitBreaker.find_alternative() (component
15) depends on this component exposing which tools are interchangeable
— not yet designed here.
"""

from dataclasses import dataclass


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
        raise NotImplementedError

    def discover_tool(self, need: str) -> list[Tool]:
        raise NotImplementedError

    def select_tool(self, need: str, candidates: list[Tool]) -> Tool:
        raise NotImplementedError

    def execute_tool(self, call: ToolCall) -> ToolResult:
        raise NotImplementedError

    def validate_result(self, result: ToolResult) -> bool:
        raise NotImplementedError

    def retry_tool(self, call: ToolCall) -> ToolResult:
        raise NotImplementedError

    def switch_tool(self, failed: Tool, need: str) -> Tool:
        raise NotImplementedError

    def interact_with_environment(self, action: dict) -> dict:
        raise NotImplementedError
