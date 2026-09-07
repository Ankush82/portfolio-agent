import uuid
from datetime import datetime, timezone
from src.cross_cutting.observability import normalize_audit_event

def test_normalize_audit_event_trade_executed():
    result = normalize_audit_event('trade.executed', {'user': 'alice', 'portfolio_id': 'p123'}, 'c10_agent_runtime')
    assert result['actor'] == {'user': 'alice'}
    assert result['component'] == 'c10_agent_runtime'
    assert result['resource'] == {'portfolio_id': 'p123'}
    assert result['action'] == 'trade.executed'
    assert result['outcome'] == 'success'
    # Check event_id is a valid UUID string
    assert isinstance(result['event_id'], str)
    uuid.UUID(result['event_id'])  # Will raise ValueError if invalid
    # Check timestamp is a datetime object in UTC
    assert isinstance(result['timestamp'], datetime)
    assert result['timestamp'].tzinfo == timezone.utc

def test_normalize_audit_event_login_actor():
    result = normalize_audit_event('login', {'actor': {'id': 'u1', 'type': 'service'}}, 'security')
    assert result['actor'] == {'id': 'u1', 'type': 'service'}

def test_normalize_audit_event_outcome_failure():
    result = normalize_audit_event('event', {'status': 'failure'}, 'module')
    assert result['outcome'] == 'failure'

def test_normalize_audit_event_action_and_metadata():
    result = normalize_audit_event('event', {'action': 'specific_action', 'extra': 'data'}, 'm')
    assert result['action'] == 'specific_action'
    assert result['metadata'] == {'extra': 'data'}

def test_normalize_audit_event_redaction():
    result = normalize_audit_event('event', {'password': 'secret', 'msg': 'hello'}, 'm')
    assert result['metadata'] == {'password': '[REDACTED]', 'msg': 'hello'}

def test_normalize_audit_event_event_id_and_timestamp():
    result = normalize_audit_event('event', {}, 'm')
    assert isinstance(result['event_id'], str)
    uuid.UUID(result['event_id'])
    assert isinstance(result['timestamp'], datetime)
    assert result['timestamp'].tzinfo == timezone.utc

def test_normalize_audit_event_empty_detail():
    result = normalize_audit_event('event', {}, 'module')
    assert result['actor'] == 'system'
    assert result['component'] == 'module'
    assert result['resource'] is None
    assert result['metadata'] == {}


import pytest
from src.cross_cutting.observability import AuditWriteError, DefaultAuditManager
from infrastructure_postgres import DefaultInfrastructure


def test_default_audit_manager_records_to_postgres():
    infrastructure = DefaultInfrastructure()
    audit_manager = DefaultAuditManager(infrastructure)

    event_type = 'test_event_for_story4'
    detail = {'user': 'alice', 'test_id': 'some_unique_id'}

    audit_manager.record(event_type, detail)

    with infrastructure._connection().cursor() as cursor:
        cursor.execute(
            """
            SELECT event_type, timestamp, actor, component, resource, action, outcome, metadata, raw_detail
            FROM audit_events
            WHERE event_type = %s
            """,
            (event_type,)
        )
        rows = cursor.fetchall()
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        row = rows[0]

        assert row[0] == event_type
        assert row[1] is not None
        assert row[2] == {'user': 'alice'}
        assert isinstance(row[3], str)
        assert len(row[3]) > 0
        assert row[4] is None
        assert row[5] == event_type
        assert row[6] == 'success'
        assert row[7] == {'test_id': 'some_unique_id'}
        assert row[8] == {'user': 'alice', 'test_id': 'some_unique_id'}

    # Clean up
    with infrastructure._connection().cursor() as cursor:
        cursor.execute(
            "DELETE FROM audit_events WHERE event_type = %s",
            (event_type,)
        )


def test_default_audit_manager_raises_on_db_unavailable():
    infrastructure_bad = DefaultInfrastructure(
        postgres_dsn="postgresql://wrong:wrong@localhost:5432/wrong"
    )
    audit_manager_bad = DefaultAuditManager(infrastructure_bad)

    with pytest.raises(AuditWriteError):
        audit_manager_bad.record('test', {})