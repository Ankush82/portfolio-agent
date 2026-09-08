"""Tests for DefaultAuditManager persisting to Postgres (STORY-4 / issue #199).

Uses the real local Postgres instance (same DSN infrastructure_postgres.py
defaults to) -- no mocks, per this codebase's DefaultInfrastructure design
("this class never hides a down Postgres... behind a fake success").
"""

import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from cross_cutting.observability import AuditWriteError, DefaultAuditManager
from infrastructure_postgres import DEFAULT_POSTGRES_DSN, DefaultInfrastructure


def _fetch_events_for(event_type: str) -> list[dict]:
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_type, actor, component, resource, action,
                       outcome, metadata, raw_detail
                FROM audit_events WHERE event_type = %s
                ORDER BY timestamp
                """,
                (event_type,),
            )
            columns = [d.name for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def _unique_event_type(prefix: str) -> str:
    return f"{prefix}.{uuid.uuid4().hex}"


def test_record_inserts_one_row_with_correct_fields():
    """AC-1/AC-2: record() inserts exactly one row with correct
    event_type, actor, component, action, outcome, metadata, raw_detail."""
    manager = DefaultAuditManager(DefaultInfrastructure())
    event_type = _unique_event_type("test")

    manager.record(event_type, {"user": "alice", "portfolio_id": "p1", "extra": "data"})

    rows = _fetch_events_for(event_type)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == event_type
    assert row["actor"] == {"user": "alice"}
    assert row["component"] == "tests.cross_cutting.test_qa_story199_default_audit_manager"
    assert row["resource"] == {"portfolio_id": "p1"}
    assert row["action"] == event_type
    assert row["outcome"] == "success"
    assert row["metadata"] == {"extra": "data"}
    assert row["raw_detail"] == {"user": "alice", "portfolio_id": "p1", "extra": "data"}


def test_record_identifies_calling_module_in_component_column():
    """AC-3: calling module is correctly identified in the component
    column when the caller doesn't supply an explicit 'component' key."""
    manager = DefaultAuditManager(DefaultInfrastructure())
    event_type = _unique_event_type("test")

    manager.record(event_type, {})

    rows = _fetch_events_for(event_type)
    assert rows[0]["component"] == __name__


def test_record_respects_explicit_component_override():
    manager = DefaultAuditManager(DefaultInfrastructure())
    event_type = _unique_event_type("test")

    manager.record(event_type, {"component": "some_other_module"})

    rows = _fetch_events_for(event_type)
    assert rows[0]["component"] == "some_other_module"


def test_record_redacts_secrets_in_raw_detail_and_metadata():
    manager = DefaultAuditManager(DefaultInfrastructure())
    event_type = _unique_event_type("test")

    manager.record(event_type, {"password": "hunter2", "note": "ok"})

    rows = _fetch_events_for(event_type)
    row = rows[0]
    assert row["metadata"] == {"password": "[REDACTED]", "note": "ok"}
    assert row["raw_detail"] == {"password": "[REDACTED]", "note": "ok"}


def test_default_constructor_with_no_args_works():
    """No changes required to any existing call site constructor calls --
    `DefaultAuditManager()` with zero args must keep working."""
    manager = DefaultAuditManager()
    event_type = _unique_event_type("test")

    manager.record(event_type, {"user": "bob"})

    rows = _fetch_events_for(event_type)
    assert len(rows) == 1
    assert rows[0]["actor"] == {"user": "bob"}


def test_record_raises_audit_write_error_when_database_unavailable():
    """AC-5: record() raises AuditWriteError if the database is unavailable."""
    unreachable = DefaultInfrastructure(
        postgres_dsn="postgresql://nouser:nopass@127.0.0.1:59999/nonexistent_db"
    )
    manager = DefaultAuditManager(unreachable)

    with pytest.raises(AuditWriteError):
        manager.record("test.unreachable", {"user": "carol"})
