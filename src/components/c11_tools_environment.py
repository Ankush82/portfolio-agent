"""Tools & Environment (component 11) — everything the agent can act
upon or interact with.

Design: ADR-0024 (registry/dispatch mechanism), ADR-0025 (tool
interchangeability closes Reliability & Resilience's
CircuitBreaker.find_alternative() gap).
Interface: <- Agent Runtime (10).

This is a registry and dispatcher, not a specific tool: `DefaultTools
Environment` below implements real bookkeeping, matching, dispatch,
and failure handling, with zero tools pre-registered — no concrete
tool (a market-data tool, a broker API tool, etc.) has been designed
anywhere in this project yet, so registering a fake one here would be
dishonest scaffolding, not a real default. See ADR-0024 for why an
empty registry is the correct default, not a gap.
"""

from dataclasses import dataclass
from typing import Callable, Protocol

from cross_cutting.observability import AuditManager, DefaultAuditManager, traced
from cross_cutting.reliability import (
    CircuitBreaker,
    DefaultCircuitBreaker,
    DefaultFailureClassifier,
    FailureClassifier,
    FailureEvent,
    FailureType,
)


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


class ToolsEnvironment(Protocol):
    def register_tool(self, tool: Tool) -> None:
        ...

    def discover_tool(self, need: str) -> list[Tool]:
        ...

    def select_tool(self, need: str, candidates: list[Tool]) -> Tool:
        ...

    def execute_tool(self, call: ToolCall) -> ToolResult:
        ...

    def validate_result(self, result: ToolResult) -> bool:
        ...

    def retry_tool(self, call: ToolCall) -> ToolResult:
        ...

    def switch_tool(self, failed: Tool, need: str) -> Tool:
        ...

    def interact_with_environment(self, action: dict) -> dict:
        ...


class StubToolsEnvironment:
    """Structural implementation of ToolsEnvironment. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def register_tool(self, tool: Tool) -> None:
        with traced("StubToolsEnvironment.register_tool"):
            return None

    def discover_tool(self, need: str) -> list[Tool]:
        with traced("StubToolsEnvironment.discover_tool"):
            return []

    def select_tool(self, need: str, candidates: list[Tool]) -> Tool:
        with traced("StubToolsEnvironment.select_tool"):
            return Tool(name="stub", schema={})

    def execute_tool(self, call: ToolCall) -> ToolResult:
        with traced("StubToolsEnvironment.execute_tool"):
            return ToolResult(call=ToolCall(tool_name="stub", arguments={}), output={}, ok=True)

    def validate_result(self, result: ToolResult) -> bool:
        with traced("StubToolsEnvironment.validate_result"):
            return True

    def retry_tool(self, call: ToolCall) -> ToolResult:
        with traced("StubToolsEnvironment.retry_tool"):
            return ToolResult(call=ToolCall(tool_name="stub", arguments={}), output={}, ok=True)

    def switch_tool(self, failed: Tool, need: str) -> Tool:
        with traced("StubToolsEnvironment.switch_tool"):
            return Tool(name="stub", schema={})

    def interact_with_environment(self, action: dict) -> dict:
        with traced("StubToolsEnvironment.interact_with_environment"):
            return {}


class DefaultToolsEnvironment:
    """Real implementation of ToolsEnvironment (ADR-0024, ADR-0025).

    Registry design (ADR-0024): `register_tool`'s signature in the
    Protocol above takes only a `Tool` — there is no second Protocol
    method for registering behavior. Rather than grow the `Tool`
    dataclass to carry a `Callable` field (which would force every
    caller of the already-defined `Tool`/`ToolCall`/`ToolResult` shapes,
    including `StubToolsEnvironment`, to know about invocation), this
    class keeps `Tool` as pure metadata and adds a parallel registry —
    `self._invocables: dict[str, Callable]` — populated through an
    extra optional parameter on `register_tool` (`invoke=`). A `Tool`
    registered without `invoke` is real and discoverable, but not
    executable; `execute_tool` reports that honestly rather than
    pretending a no-op succeeded.

    Both the tool registry and the invocable registry are plain
    in-memory dicts, not `DefaultInfrastructure`-backed. Tool
    registration is a runtime/process-lifetime concern — which tools a
    running agent process currently has wired up — not durable state
    that needs to survive a restart; nothing in this project's design
    says otherwise. That is a real judgment call, documented here
    rather than defaulted into silently.

    Selection and interchangeability (ADR-0024, ADR-0025) both use the
    same rule: two tools are related to a `need` (or to each other) if
    they share a capability tag in `Tool.schema["tags"]`, or if the
    need equals the tool's name outright. `alternatives_for()` is the
    concrete interchangeability mapping Reliability & Resilience's
    `CircuitBreaker.find_alternative()` was left waiting on (ADR-0016) —
    see `__init__` for how it's wired into this instance's own
    `DefaultCircuitBreaker` by default.
    """

    def __init__(
        self,
        circuit_breaker: CircuitBreaker | None = None,
        failure_classifier: FailureClassifier | None = None,
        audit_manager: AuditManager | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._invocables: dict[str, Callable[[dict], dict]] = {}
        self._environment_adapters: dict[str, Callable[[dict], dict]] = {}
        self._failure_history: dict[str, list[FailureEvent]] = {}
        self._failure_classifier = failure_classifier or DefaultFailureClassifier()
        self._audit_manager = audit_manager or DefaultAuditManager()
        # When no circuit breaker is injected, this constructs its own
        # DefaultCircuitBreaker wired with THIS registry's
        # alternatives_for as its alternative_source — the concrete
        # closing of the ADR-0016 gap (ADR-0025). A caller supplying
        # its own circuit breaker (e.g. one shared across components)
        # is responsible for wiring alternative_source itself if it
        # wants this registry's interchangeability data available to
        # find_alternative().
        self._circuit_breaker = circuit_breaker or DefaultCircuitBreaker(
            alternative_source=self.alternatives_for
        )

    @staticmethod
    def _matches(need: str, tool: Tool) -> bool:
        """The one matching rule used throughout this class (ADR-0024):
        a tool matches a need if the need is the tool's exact name, or
        the need appears in the tool's declared capability tags
        (`Tool.schema["tags"]`, a list of strings)."""
        return need == tool.name or need in tool.schema.get("tags", [])

    def register_tool(self, tool: Tool, invoke: Callable[[dict], dict] | None = None) -> None:
        """`invoke`, if given, is the callable this tool actually
        dispatches to on `execute_tool` — arguments in, output dict
        out. Registering the same `tool.name` again replaces both the
        metadata and the invocable, matching ordinary dict semantics
        rather than silently keeping the old entry."""
        with traced("DefaultToolsEnvironment.register_tool"):
            self._tools[tool.name] = tool
            self._invocables[tool.name] = invoke

    def discover_tool(self, need: str) -> list[Tool]:
        with traced("DefaultToolsEnvironment.discover_tool"):
            return [tool for tool in self._tools.values() if self._matches(need, tool)]

    def select_tool(self, need: str, candidates: list[Tool]) -> Tool:
        """Exact name match wins over a tag match; the first match
        wins any remaining tie, in the order `candidates` was given.
        No match is a real `ValueError`, not a silent arbitrary pick —
        guessing among non-matching tools would be cognition this
        component doesn't have, not selection logic."""
        with traced("DefaultToolsEnvironment.select_tool"):
            if not candidates:
                raise ValueError(f"select_tool: no candidates supplied for need {need!r}")
            exact_name_matches = [tool for tool in candidates if tool.name == need]
            if exact_name_matches:
                return exact_name_matches[0]
            tag_matches = [tool for tool in candidates if need in tool.schema.get("tags", [])]
            if tag_matches:
                return tag_matches[0]
            raise ValueError(f"select_tool: no candidate matches need {need!r} by name or tag")

    def execute_tool(self, call: ToolCall) -> ToolResult:
        """Real dispatch: looks up the registered invocable for
        `call.tool_name` and actually calls it, wrapped with the real
        `DefaultCircuitBreaker`/`DefaultFailureClassifier` from
        `cross_cutting.reliability` so a failing tool gets classified
        and can actually trip (ADR-0024). Every attempt and its
        outcome is recorded via `AuditManager` — exactly the kind of
        event worth an audit trail."""
        with traced("DefaultToolsEnvironment.execute_tool"):
            self._audit_manager.record(
                "tool_execution_attempt",
                {"tool": call.tool_name, "arguments": call.arguments},
            )

            if call.tool_name not in self._tools:
                return self._failed_result(call, "unknown_tool", f"unknown tool {call.tool_name!r}")

            if not self._circuit_breaker.is_available(call.tool_name):
                return self._failed_result(
                    call, "circuit_open", f"circuit breaker open for tool {call.tool_name!r}"
                )

            invoke = self._invocables.get(call.tool_name)
            if invoke is None:
                return self._failed_result(
                    call, "no_invocable", f"no invocable registered for tool {call.tool_name!r}"
                )

            try:
                output = invoke(call.arguments)
            except Exception as exc:  # noqa: BLE001 — any tool failure must be classified, not just some
                return self._handle_tool_exception(call, exc)

            result = ToolResult(call=call, output=output, ok=True)
            self._audit_manager.record(
                "tool_execution_result", {"tool": call.tool_name, "ok": True}
            )
            return result

    def _failed_result(self, call: ToolCall, reason: str, message: str) -> ToolResult:
        self._audit_manager.record(
            "tool_execution_result",
            {"tool": call.tool_name, "ok": False, "reason": reason},
        )
        return ToolResult(call=call, output={"error": message}, ok=False)

    def _handle_tool_exception(self, call: ToolCall, exc: Exception) -> ToolResult:
        """Classifies the failure via `DefaultFailureClassifier` using
        this tool's own trailing failure history, then trips the
        circuit breaker if that history now reads as a loop or cascade
        (ADR-0015/ADR-0016) rather than treating every failure as a
        one-off transient."""
        history = self._failure_history.setdefault(call.tool_name, [])
        event = FailureEvent(
            component="tools_environment", tool=call.tool_name, error=str(exc), history=list(history)
        )
        failure_type = self._failure_classifier.classify(event)
        history.append(event)

        if failure_type == FailureType.LOOP_OR_CASCADE:
            self._circuit_breaker.trip(call.tool_name)

        self._audit_manager.record(
            "tool_execution_result",
            {
                "tool": call.tool_name,
                "ok": False,
                "reason": "exception",
                "error": str(exc),
                "failure_type": failure_type.name,
            },
        )
        return ToolResult(
            call=call, output={"error": str(exc), "failure_type": failure_type.name}, ok=False
        )

    def validate_result(self, result: ToolResult) -> bool:
        """Structural check only, per this component's scope: real
        `ok`, a real dict `output`, and a real bound `call` with a
        non-empty tool name. Never inspects `output`'s contents for
        correctness — that is cognition this component does not do."""
        with traced("DefaultToolsEnvironment.validate_result"):
            return (
                result.ok is True
                and isinstance(result.output, dict)
                and result.call is not None
                and bool(result.call.tool_name)
            )

    def retry_tool(self, call: ToolCall) -> ToolResult:
        """Re-invokes via the exact same dispatch path as
        `execute_tool` — not a separate code path — so circuit-breaker
        state is respected automatically: `execute_tool` already
        refuses to invoke a tripped tool (see the `circuit_open`
        branch above), which is what makes this retry never bypass a
        tripped tool rather than needing its own duplicate check."""
        with traced("DefaultToolsEnvironment.retry_tool"):
            return self.execute_tool(call)

    def alternatives_for(self, tool_name: str) -> list[str]:
        """The tool-interchangeability mapping Reliability &
        Resilience's `CircuitBreaker.find_alternative()` and Agent
        Runtime's `DefaultRecoveryManager` were left waiting on
        (ADR-0016's Consequences; ADR-0025 closes it). Two registered
        tools are alternatives for each other if they share at least
        one capability tag in `Tool.schema["tags"]`. Not part of the
        `ToolsEnvironment` Protocol — a real accessor other components
        call directly (or, for `DefaultCircuitBreaker`, via the
        `alternative_source` callable this class wires in `__init__`),
        the same shape as `DefaultTaskManager.status()` in Agent
        Runtime."""
        with traced("DefaultToolsEnvironment.alternatives_for"):
            tool = self._tools.get(tool_name)
            if tool is None:
                return []
            tags = set(tool.schema.get("tags", []))
            if not tags:
                return []
            return [
                other.name
                for other in self._tools.values()
                if other.name != tool_name and tags & set(other.schema.get("tags", []))
            ]

    def switch_tool(self, failed: Tool, need: str) -> Tool:
        """Picks a real replacement for `failed` using `alternatives_for`,
        filtered to tools the circuit breaker currently reports
        available and that still satisfy `need` by this class's one
        matching rule. Raises `ValueError` — a real, visible failure —
        rather than silently returning `failed` again or an arbitrary
        registered tool when no usable alternative exists."""
        with traced("DefaultToolsEnvironment.switch_tool"):
            available_alternatives = [
                self._tools[name]
                for name in self.alternatives_for(failed.name)
                if name in self._tools and self._circuit_breaker.is_available(name)
            ]
            need_matching = [tool for tool in available_alternatives if self._matches(need, tool)]
            candidates = need_matching or available_alternatives
            if not candidates:
                raise ValueError(
                    f"switch_tool: no available alternative to {failed.name!r} for need {need!r}"
                )
            replacement = self.select_tool(need, candidates)
            self._audit_manager.record(
                "tool_switch",
                {"failed_tool": failed.name, "replacement_tool": replacement.name, "need": need},
            )
            return replacement

    def register_environment_adapter(self, name: str, adapter: Callable[[dict], dict]) -> None:
        """Not part of the `ToolsEnvironment` Protocol — the real
        registration hook `interact_with_environment` dispatches
        through. No concrete environment has been designed anywhere in
        this project yet, so zero adapters are registered by default;
        same reasoning as the empty tool registry (ADR-0024)."""
        with traced("DefaultToolsEnvironment.register_environment_adapter"):
            self._environment_adapters[name] = adapter

    def interact_with_environment(self, action: dict) -> dict:
        """Generic pass-through to whichever adapter `action["adapter"]`
        names. With zero adapters registered, this is honestly a real
        `KeyError`/`ValueError`, not a fabricated empty-dict response —
        the mechanism is real even though nothing is plugged into it
        yet."""
        with traced("DefaultToolsEnvironment.interact_with_environment"):
            adapter_name = action.get("adapter")
            if not adapter_name:
                raise ValueError("interact_with_environment: action must include an 'adapter' key")
            adapter = self._environment_adapters.get(adapter_name)
            if adapter is None:
                raise KeyError(
                    f"interact_with_environment: no environment adapter registered for {adapter_name!r}"
                )
            self._audit_manager.record(
                "environment_interaction", {"adapter": adapter_name, "action": action}
            )
            return adapter(action)
