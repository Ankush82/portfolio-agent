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


class FailureClassifier:
    def classify(self, event: FailureEvent) -> FailureType:
        """Fig. 15.1's 'failure type?' branch. Detection mechanism for
        distinguishing transient from loop/cascade is not yet designed
        — see ADR-0015's consequences."""
        with traced("FailureClassifier.classify"):
            return FailureType.TRANSIENT


class CircuitBreaker:
    """Scope: per tool (ADR-0016), not per trajectory."""

    def trip(self, tool_name: str) -> None:
        """Marks a tool temporarily unavailable."""
        with traced("CircuitBreaker.trip"):
            return None

    def is_available(self, tool_name: str) -> bool:
        with traced("CircuitBreaker.is_available"):
            return True

    def find_alternative(self, tool_name: str) -> str | None:
        """Fig. 15.1's 'alternative exists?' branch. The mapping of
        which tools are interchangeable is not yet designed — depends
        on Tools & Environment (component 11)."""
        with traced("CircuitBreaker.find_alternative"):
            return None
