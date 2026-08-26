"""Reliability & Resilience (component 15) — a constraint applied to
every hop in the system, not a component every hop implements itself.

Design: Phase 0 Cross-Cutting Design, fig. 15.1
Decisions:
  ADR-0015 — failure classification; partially supersedes ADR-0004
             (Agent Runtime's replan-first default no longer applies
             unconditionally)
  ADR-0016 — circuit breaker scope: per tool, not per trajectory

Every component's Executor-equivalent calls FailureClassifier.classify()
on any step failure before deciding what happens next. Transient
failures still go to that component's own replan/recovery logic,
unchanged. Loop/cascade patterns get routed to CircuitBreaker instead.
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

from cross_cutting.observability import traced


class FailureType(Enum):
    TRANSIENT = auto()
    LOOP_OR_CASCADE = auto()


@dataclass
class FailureEvent:
    component: str
    tool: str | None
    error: str
    history: list["FailureEvent"]


class FailureClassifier(Protocol):
    def classify(self, event: FailureEvent) -> FailureType:
        """Fig. 15.1's 'failure type?' branch. Detection mechanism for
        distinguishing transient from loop/cascade is not yet designed
        — see ADR-0015's consequences."""
        ...


class StubFailureClassifier:
    """Structural implementation of FailureClassifier. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def classify(self, event: FailureEvent) -> FailureType:
        with traced("StubFailureClassifier.classify"):
            return FailureType.TRANSIENT


class DefaultFailureClassifier:
    """Real implementation of FailureClassifier (ADR-0015). Detection
    rule: the most recent entries in event.history form a tight,
    same-component run — the last _LOOP_WINDOW entries were all
    failures of the same component as the current event. That's a loop
    or cascade; anything else is transient and still goes to that
    component's own replan/recovery logic, per ADR-0015."""

    _LOOP_WINDOW = 3

    def classify(self, event: FailureEvent) -> FailureType:
        with traced("DefaultFailureClassifier.classify"):
            if len(event.history) < self._LOOP_WINDOW:
                return FailureType.TRANSIENT
            recent = event.history[-self._LOOP_WINDOW:]
            if all(entry.component == event.component for entry in recent):
                return FailureType.LOOP_OR_CASCADE
            return FailureType.TRANSIENT


class CircuitBreaker(Protocol):
    """Scope: per tool (ADR-0016), not per trajectory."""

    def trip(self, tool_name: str) -> None:
        """Marks a tool temporarily unavailable."""
        ...

    def is_available(self, tool_name: str) -> bool:
        ...

    def find_alternative(self, tool_name: str) -> str | None:
        """Fig. 15.1's 'alternative exists?' branch. The mapping of
        which tools are interchangeable is not yet designed — depends
        on Tools & Environment (component 11)."""
        ...


class StubCircuitBreaker:
    """Structural implementation of CircuitBreaker. Every method is a
    traced no-op — see cross_cutting/observability.py."""

    def trip(self, tool_name: str) -> None:
        with traced("StubCircuitBreaker.trip"):
            return None

    def is_available(self, tool_name: str) -> bool:
        with traced("StubCircuitBreaker.is_available"):
            return True

    def find_alternative(self, tool_name: str) -> str | None:
        with traced("StubCircuitBreaker.find_alternative"):
            return None


class DefaultCircuitBreaker:
    """Real implementation of CircuitBreaker (ADR-0016): per-tool scope,
    with real trip state kept in this instance.

    The tool-interchangeability mapping find_alternative would need is
    not yet designed — it depends on Tools & Environment (component 11,
    per the CircuitBreaker protocol docstring). Rather than invent one,
    this accepts an optional caller-supplied mapping and is honest that
    there is no alternative when nothing has been configured.
    """

    def __init__(self, alternatives: dict[str, str] | None = None) -> None:
        self._tripped_tools: set[str] = set()
        self._alternatives = alternatives or {}

    def trip(self, tool_name: str) -> None:
        with traced("DefaultCircuitBreaker.trip"):
            self._tripped_tools.add(tool_name)

    def is_available(self, tool_name: str) -> bool:
        with traced("DefaultCircuitBreaker.is_available"):
            return tool_name not in self._tripped_tools

    def find_alternative(self, tool_name: str) -> str | None:
        with traced("DefaultCircuitBreaker.find_alternative"):
            return self._alternatives.get(tool_name)
