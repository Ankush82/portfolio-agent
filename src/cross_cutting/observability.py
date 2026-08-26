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

Blueprint stage: `traced()` below is real, minimal logic — the one
exception to "no logic in the blueprint" — because tracing every stub
call is the entire point of a runnable blueprint: it's how you watch
the architecture actually execute before any component has real
behavior. Everything downstream of this file stays a traced no-op.
"""

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

TRACE_LOG_PATH = Path("trace.log")


@dataclass
class Span:
    name: str
    parent: "Span | None" = None
    metrics: dict = field(default_factory=dict)

    def record_metric(self, name: str, value: float) -> None:
        self.metrics[name] = value

    def record_cost(self, tokens_in: int, tokens_out: int) -> None:
        self.metrics["tokens_in"] = tokens_in
        self.metrics["tokens_out"] = tokens_out


def _write_trace_line(line: str) -> None:
    print(line)
    with TRACE_LOG_PATH.open("a") as f:
        f.write(line + "\n")


@contextmanager
def traced(name: str, parent: Span | None = None):
    """Every component call is wrapped: `with traced("Component.method")
    as span:`. Writes a start and finish line to trace.log (and stdout)
    so a run of the blueprint produces a readable record of which
    component called what, in what order."""
    span = Span(name=name, parent=parent)
    _write_trace_line(f"{time.strftime('%H:%M:%S')} | {name} | started")
    try:
        yield span
    finally:
        _write_trace_line(f"{time.strftime('%H:%M:%S')} | {name} | finished {span.metrics}")


class AuditManager:
    def record(self, event_type: str, detail: dict) -> None:
        """Audit-relevant events: quarantine decisions (Memory, fig.
        1), blocked claims (Evidence & Verification, fig. 2), circuit
        breaker trips (Reliability & Resilience, fig. 15.1)."""
        with traced(f"AuditManager.record[{event_type}]"):
            return None
