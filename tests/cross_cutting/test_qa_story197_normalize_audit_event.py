"""Tests for normalize_audit_event (STORY-2 / issue #197).

Each test mirrors one of the issue's own acceptance-criteria examples
directly, plus dataclass/type invariants (event_id is a real UUID,
timestamp is a real datetime).
"""

import uuid
from datetime import datetime, timezone

from src.cross_cutting.observability import normalize_audit_event


def test_actor_and_resource_wrap_the_matched_detail_key():
    """AC-1: {'user': 'alice', 'portfolio_id': 'p123'} -> actor={'user':
    'alice'}, resource={'portfolio_id': 'p123'}, component=calling_module,
    action=event_type, outcome='success'."""
    event = normalize_audit_event(
        "trade.executed", {"user": "alice", "portfolio_id": "p123"}, "c10_agent_runtime"
    )
    assert event["actor"] == {"user": "alice"}
    assert event["component"] == "c10_agent_runtime"
    assert event["resource"] == {"portfolio_id": "p123"}
    assert event["action"] == "trade.executed"
    assert event["outcome"] == "success"


def test_explicit_actor_key_used_as_is():
    """AC-2: a real {'actor': {...}} value is used directly, not re-wrapped."""
    event = normalize_audit_event(
        "login", {"actor": {"id": "u1", "type": "service"}}, "security"
    )
    assert event["actor"] == {"id": "u1", "type": "service"}


def test_outcome_from_status_key():
    """AC-3: outcome falls back to detail['status'] when 'outcome' is absent."""
    event = normalize_audit_event("event", {"status": "failure"}, "module")
    assert event["outcome"] == "failure"


def test_explicit_action_and_extra_key_goes_to_metadata():
    """AC-4: an explicit action is used as-is (a flat string, never wrapped);
    any key that isn't one of the known extraction fields lands in metadata."""
    event = normalize_audit_event(
        "event", {"action": "specific_action", "extra": "data"}, "m"
    )
    assert event["action"] == "specific_action"
    assert event["metadata"] == {"extra": "data"}


def test_secret_looking_metadata_key_is_redacted():
    """AC-5: redact_secrets is applied to metadata."""
    event = normalize_audit_event("event", {"password": "secret", "msg": "hello"}, "m")
    assert event["metadata"] == {"password": "[REDACTED]", "msg": "hello"}


def test_event_id_is_a_real_uuid_and_timestamp_is_utc_datetime():
    """AC-6: event_id parses as a real UUID; timestamp is a real,
    timezone-aware UTC datetime object, not a string."""
    event = normalize_audit_event("event", {}, "m")
    uuid.UUID(event["event_id"])  # raises ValueError if not a real UUID
    assert isinstance(event["timestamp"], datetime)
    assert event["timestamp"].tzinfo == timezone.utc


def test_empty_detail_produces_all_defaults():
    """AC-7: an empty detail dict still produces a fully-populated,
    valid event using every documented default."""
    event = normalize_audit_event("event", {}, "calling_module_x")
    assert event["actor"] == "system"
    assert event["component"] == "calling_module_x"
    assert event["resource"] is None
    assert event["action"] == "event"
    assert event["outcome"] == "success"
    assert event["metadata"] == {}


def test_actor_priority_actor_over_user_over_entity():
    """When multiple actor candidates are present, 'actor' wins over
    'user', which wins over 'entity' -- and all three candidate keys are
    removed from metadata regardless of which one matched."""
    event = normalize_audit_event(
        "event", {"actor": {"id": "real"}, "user": "ignored", "entity": "also-ignored"}, "m"
    )
    assert event["actor"] == {"id": "real"}
    assert "user" not in event["metadata"]
    assert "entity" not in event["metadata"]
