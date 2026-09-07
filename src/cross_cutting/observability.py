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

import inspect
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


class AuditWriteError(RuntimeError):
    """Raised by DefaultAuditManager.record() when the audit event
    cannot be persisted (STORY-4) -- e.g. the database is unavailable."""


class DefaultAuditManager(AuditManager):
    """Real implementation of AuditManager: persists each event as one
    row in the `audit_events` Postgres table via Infrastructure
    (STORY-4). Defaults to a real `DefaultInfrastructure()` when none is
    injected, so every existing zero-arg `DefaultAuditManager()` call
    site keeps working unchanged."""

    def __init__(self, infrastructure=None) -> None:
        if infrastructure is None:
            from infrastructure_postgres import DefaultInfrastructure
            infrastructure = DefaultInfrastructure()
        self._infrastructure = infrastructure

    def record(self, event_type: str, detail: dict) -> None:
        caller_frame = inspect.stack()[1]
        caller_module = inspect.getmodule(caller_frame.frame)
        calling_module = caller_module.__name__ if caller_module is not None else caller_frame.filename

        with traced(f"DefaultAuditManager.record[{event_type}]"):
            event = normalize_audit_event(event_type, detail, calling_module)
            try:
                self._infrastructure.record_audit_event(event, redact_secrets(detail))
            except Exception as exc:
                raise AuditWriteError(
                    f"failed to persist audit event {event_type!r}: {exc}"
                ) from exc


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


# Every detail key any of actor/component/resource/action/outcome can be
# sourced from (STORY-2) -- all removed from metadata together, regardless
# of which single one actually matched, since they're all "known fields"
# conceptually, not just the winning candidate.
_KNOWN_DETAIL_KEYS = frozenset({
    "actor", "user", "entity",
    "component",
    "resource", "portfolio_id", "agent_id",
    "action",
    "outcome", "status",
})


def normalize_audit_event(event_type: str, detail: dict, calling_module: str) -> dict:
    """Produce a structured audit event dict from a raw ``record(event_type,
    detail)`` call (STORY-2) -- the shape ``DefaultAuditManager.record()``
    persists, and the shape ``DefaultAuditReader.query()`` reads back.

    Extraction rules, each checked in the order given (first match wins),
    with a default when none match:
      - ``actor``: ``detail['actor']`` used AS-IS if present (it's already
        real actor data); else ``{'user': detail['user']}`` if present;
        else ``{'entity': detail['entity']}`` if present; else the string
        ``'system'``. The key name is preserved in the wrapped dict so a
        caller populating "identity" can tell whether it was passed as
        `user` or `entity` without needing a second field.
      - ``component``: ``detail['component']`` if present, else the real
        ``calling_module`` the caller identified via `inspect.stack()`.
      - ``resource``: ``detail['resource']`` used AS-IS if present; else
        ``{'portfolio_id': ...}``; else ``{'agent_id': ...}``; else
        ``None`` (many events genuinely have no single resource).
      - ``action``: ``detail['action']`` if present, else the raw
        ``event_type`` string itself.
      - ``outcome``: ``detail['outcome']`` if present, else
        ``detail['status']``; else ``'success'`` (most calls into
        ``record()`` today are logging a thing that already happened
        successfully, not a failure being reported).
      - ``metadata``: every OTHER detail key (i.e. not one of
        ``_KNOWN_DETAIL_KEYS`` above), with ``redact_secrets`` applied so
        a caller that put a real credential in `detail` never has it
        persisted in plain text.

    ``event_id`` is a fresh real UUID4 string; ``timestamp`` is a real,
    timezone-aware UTC ``datetime`` (not a string -- callers that persist
    this to a `TIMESTAMPTZ` column want a real datetime object, not a
    pre-formatted string they'd have to re-parse)."""
    if "actor" in detail:
        actor = detail["actor"]
    elif "user" in detail:
        actor = {"user": detail["user"]}
    elif "entity" in detail:
        actor = {"entity": detail["entity"]}
    else:
        actor = "system"

    component = detail.get("component", calling_module)

    if "resource" in detail:
        resource = detail["resource"]
    elif "portfolio_id" in detail:
        resource = {"portfolio_id": detail["portfolio_id"]}
    elif "agent_id" in detail:
        resource = {"agent_id": detail["agent_id"]}
    else:
        resource = None

    action = detail.get("action", event_type)
    outcome = detail.get("outcome", detail.get("status", "success"))

    metadata = redact_secrets({k: v for k, v in detail.items() if k not in _KNOWN_DETAIL_KEYS})

    return {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc),
        "event_type": event_type,
        "actor": actor,
        "component": component,
        "resource": resource,
        "action": action,
        "outcome": outcome,
        "metadata": metadata,
    }
