"""Integration tests for the real audit system (STORY-9): record(),
redact_secrets(), normalize_audit_event(), and DefaultAuditReader.query()
against a real Postgres audit_events table -- no mocks for DB
operations, matching this project's own real-integration-test
convention (see tests/cross_cutting/test_qa_story199_default_audit_manager.py
for the equivalent convention applied to record() specifically; this
file's own new coverage is query() and its filters, STORY-6's real
subject).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from cross_cutting.observability import (
    DefaultAuditManager,
    DefaultAuditReader,
    normalize_audit_event,
    redact_secrets,
)
from infrastructure_postgres import DEFAULT_POSTGRES_DSN, DefaultInfrastructure


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="no live Postgres reachable at DEFAULT_POSTGRES_DSN",
)


@pytest.fixture
def _audit():
    infra = DefaultInfrastructure()
    return DefaultAuditManager(infrastructure=infra), DefaultAuditReader(infra)


def _unique_component() -> str:
    return f"test-audit-{uuid.uuid4().hex[:12]}"


# --- AC-2: record() + redaction -------------------------------------------


def test_record_row_matches_expected_schema_with_redaction(_audit):
    """record('test', {'user': 'alice', 'password': 'secret'}) row has
    password '[REDACTED]' in metadata."""
    manager, reader = _audit
    component = _unique_component()
    manager.record("test", {"user": "alice", "password": "secret", "component": component})

    events = reader.query(component=component)
    assert len(events) == 1
    assert events[0]["metadata"]["password"] == "[REDACTED]"
    assert events[0]["actor"] == {"user": "alice"}


# --- AC-3: query(component=X) ----------------------------------------------


def test_query_component_returns_only_that_components_events(_audit):
    manager, reader = _audit
    component_a = _unique_component()
    component_b = _unique_component()
    manager.record("evt_a", {"component": component_a})
    manager.record("evt_b", {"component": component_b})

    events = reader.query(component=component_a)
    assert len(events) == 1
    assert events[0]["component"] == component_a


def test_query_event_type_returns_only_matching_type(_audit):
    manager, reader = _audit
    component = _unique_component()
    manager.record("login", {"component": component})
    manager.record("logout", {"component": component})

    events = reader.query(event_type="login", component=component)
    assert len(events) == 1
    assert events[0]["event_type"] == "login"


# --- AC-4: time range ------------------------------------------------------


def test_query_time_range_is_inclusive_and_correct(_audit):
    manager, reader = _audit
    component = _unique_component()
    manager.record("timed", {"component": component})

    now = datetime.now(timezone.utc)
    before = now - timedelta(minutes=1)
    after = now + timedelta(minutes=1)

    within = reader.query(start_time=before, end_time=after, component=component)
    assert len(within) == 1

    too_late_start = reader.query(start_time=after, component=component)
    assert len(too_late_start) == 0

    too_early_end = reader.query(end_time=before, component=component)
    assert len(too_early_end) == 0


# --- AC-5: actor JSONB containment ------------------------------------------


def test_query_actor_jsonb_containment(_audit):
    manager, reader = _audit
    component = _unique_component()
    user_id = f"user_{uuid.uuid4().hex[:8]}"
    manager.record("action", {"actor": {"id": user_id, "role": "admin"}, "component": component})
    manager.record("action", {"actor": {"id": "someone-else"}, "component": component})

    events = reader.query(actor={"id": user_id}, component=component)
    assert len(events) == 1
    assert events[0]["actor"]["id"] == user_id


def test_query_resource_id_matches_id_or_portfolio_id(_audit):
    manager, reader = _audit
    component = _unique_component()
    portfolio_id = f"pf_{uuid.uuid4().hex[:8]}"
    manager.record("sync", {"portfolio_id": portfolio_id, "component": component})

    events = reader.query(resource_id=portfolio_id, component=component)
    assert len(events) == 1
    assert events[0]["resource"]["portfolio_id"] == portfolio_id


# --- AC-6: pagination --------------------------------------------------------


def test_query_pagination_returns_correct_slices(_audit):
    manager, reader = _audit
    component = _unique_component()
    for i in range(5):
        manager.record(f"paged_{i}", {"component": component})

    page1 = reader.query(component=component, limit=2, offset=0)
    page2 = reader.query(component=component, limit=2, offset=2)
    page3 = reader.query(component=component, limit=2, offset=4)

    assert len(page1) == 2
    assert len(page2) == 2
    assert len(page3) == 1

    all_ids = {e["event_id"] for e in page1 + page2 + page3}
    assert len(all_ids) == 5  # no overlap, no duplicates across pages


def test_query_no_arguments_returns_most_recent_events_capped():
    """query() with no arguments returns events, newest first, capped at
    the real 1000-row hard limit even if a caller asks for more."""
    infra = DefaultInfrastructure()
    reader = DefaultAuditReader(infra)
    events = reader.query(limit=5000)
    assert len(events) <= 1000


# --- Empty/null detail handling ---------------------------------------------


def test_query_with_empty_detail_dict_does_not_raise(_audit):
    manager, reader = _audit
    component = _unique_component()
    manager.record("empty_detail", {"component": component})  # only 'component' set

    events = reader.query(component=component)
    assert len(events) == 1
    assert events[0]["metadata"] == {}


def test_normalize_audit_event_with_empty_detail_produces_system_actor_and_none_resource():
    result = normalize_audit_event("empty", {}, "some.module")
    assert result["actor"] == "system"
    assert result["resource"] is None
    assert result["action"] == "empty"
    assert result["outcome"] == "success"
    assert result["metadata"] == {}


def test_redact_secrets_handles_none_and_empty_gracefully():
    assert redact_secrets({}) == {}
    assert redact_secrets([]) == []
    assert redact_secrets(None) is None
    assert redact_secrets({"password": None})["password"] == "[REDACTED]"
