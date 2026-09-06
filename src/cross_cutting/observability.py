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

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

TRACE_LOG_PATH = Path("trace.log")
AUDIT_LOG_PATH = Path("audit.log")


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


class AuditManager(Protocol):
    """Audit-relevant events: quarantine decisions (Memory, fig. 1),
    blocked claims (Evidence & Verification, fig. 2), circuit breaker
    trips (Reliability & Resilience, fig. 15.1)."""

    def record(self, event_type: str, detail: dict) -> None: ...


class AuditReader(Protocol):
    """Protocol for reading audit events, intended for operators and
    investigative tools rather than routine component logic.

    Obtain an instance via ``infrastructure.get_audit_reader()`` where
    ``infrastructure`` is the dependency-injected Infrastructure instance
    passed to components via constructor injection (the same pattern
    used for ``AuditManager`` and other infrastructure services).

    Example usage::

        infrastructure = self._infrastructure  # injected via constructor
        reader = infrastructure.get_audit_reader()
        events = reader.query(event_type="quarantine", limit=50)
    """

    def query(
        self,
        event_type: str | None = None,
        actor: dict | None = None,
        component: str | None = None,
        resource_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]: ...


class StubAuditManager(AuditManager):
    """Structural implementation of AuditManager. Every method is a
    traced no-op — see cross_cutting/observability.py."""
    pass


class StubAuditReader(AuditReader):
    """Structural implementation of AuditReader. Every method is a
    traced no-op — see cross_cutting/observability.py."""
    pass


class DefaultAuditManager(AuditManager):
    """Real implementation of AuditManager: appends each event as one JSON line to AUDIT_LOG_PATH."""

    def record(self, event_type: str, detail: dict) -> None:
        with traced(f"DefaultAuditManager.record[{event_type}]"):
            line = json.dumps(
                {
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "event_type": event_type,
                    "detail": detail,
                }
            )
            with AUDIT_LOG_PATH.open("a") as f:
                f.write(line + "\n")


class DefaultAuditReader(AuditReader):
    """Production implementation of AuditReader: reads from AUDIT_LOG_PATH.

    This is the real implementation operators should use for investigative
    queries and ad-hoc analysis. Routine component logic should use the
    AuditManager interface to emit events instead.

    To obtain an instance, call infrastructure.get_audit_reader() where
    ``infrastructure`` is the Infrastructure object available via the existing
    dependency-injection pattern (e.g., passed into your function/class
    constructor, retrieved from the application container).

    Minimal usage example::

        reader = infrastructure.get_audit_reader(); events = reader.query(component="x")

    Enforces AUDIT_MAX_QUERY_LIMIT as a hard ceiling on the number of
    rows returned per call, and uses AUDIT_DEFAULT_LIMIT when no explicit
    limit is provided.
    """

    def query(self, **kwargs) -> list[dict]:
        with traced("DefaultAuditReader.query"):
            # Pull out what we care about; ignore any unknown kwargs so
            # future Protocol-extended parameters are handled gracefully.
            from src.config import AUDIT_MAX_QUERY_LIMIT, AUDIT_DEFAULT_LIMIT

            event_type = kwargs.get("event_type")
            component = kwargs.get("component")
            resource_id = kwargs.get("resource_id")
            limit = kwargs.get("limit", AUDIT_DEFAULT_LIMIT)
            offset = kwargs.get("offset", 0)

            effective_limit = min(limit, AUDIT_MAX_QUERY_LIMIT)

            if not AUDIT_LOG_PATH.exists():
                return []

            events: list[dict] = []
            for line in AUDIT_LOG_PATH.read_text().splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

            # Apply basic filters
            if event_type is not None:
                events = [e for e in events if e.get("event_type") == event_type]
            if component is not None:
                events = [e for e in events if e.get("detail", {}).get("component") == component]
            if resource_id is not None:
                events = [e for e in events if e.get("detail", {}).get("resource_id") == resource_id]

            return events[offset : offset + effective_limit]


# ---------------------------------------------------------------------------
# redact_secrets — masks sensitive values in nested dicts/lists
# ---------------------------------------------------------------------------

_SECRET_KEY_SUBSTRINGS = frozenset({
    "password", "secret", "token", "credential", "api_key", "apikey",
    "auth", "bearer", "private_key", "access_key", "secret_key",
})


def redact_secrets(data: Any) -> Any:
    """Recursively walk *data*, replacing values of keys whose name contains
    a secret-related substring with the literal string ``'[REDACTED]'``.
    Case-insensitive. Dicts, lists, and all other values pass through
    unchanged. The input is never mutated."""
    if isinstance(data, dict):
        return {
            k: ("[REDACTED]" if _is_secret_key(k) else redact_secrets(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact_secrets(item) for item in data]
    return data


def _is_secret_key(key: str) -> bool:
    key_lower = key.lower()
    return any(substr in key_lower for substr in _SECRET_KEY_SUBSTRINGS)
