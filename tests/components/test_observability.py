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