"""Observability & Governance (component 16) — infrastructure-level
tracing only, for now.

Design: Phase 0 Cross-Cutting Design, fig. 16.1
Decision: ADR-0017 — infrastructure tier only; post-hoc evaluation and
predictive monitoring are explicitly out of scope for this pass.

This module also has to actually emit the drift signals three earlier
designs already promised it would watch:
  Agent Runtime (10):        cost, latency, checkpoint count per trajectory
  Memory (06):                 corroboration rate, eviction/re-retrieval thrashing
  Retrieval & Evidence (05/09): block rate, corrective-retrieval rate,
                               repeated source disagreement
This module produces that data. It does not evaluate or alert on it —
that's out of scope until a later tier is designed.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Span:
    name: str
    parent: "Span | None" = None
    metrics: dict = field(default_factory=dict)

    def record_metric(self, name: str, value: float) -> None:
        raise NotImplementedError

    def record_cost(self, tokens_in: int, tokens_out: int) -> None:
        raise NotImplementedError


@contextmanager
def traced(name: str, parent: Span | None = None):
    """Every component call should be wrapped:
    `with traced("component.method") as span:`.
    Emits a structured log entry and an execution record on exit."""
    raise NotImplementedError


class AuditManager:
    def record(self, event_type: str, detail: dict) -> None:
        """Audit-relevant events: quarantine decisions (Memory, fig.
        1), blocked claims (Evidence & Verification, fig. 2), circuit
        breaker trips (Reliability & Resilience, fig. 15.1)."""
        raise NotImplementedError
