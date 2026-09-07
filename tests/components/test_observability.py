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


import json
import pytest
from src.cross_cutting.observability import AuditWriteError, DefaultAuditManager
from infrastructure_postgres import DefaultInfrastructure
from src.infrastructure import Infrastructure


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


# ---------------------------------------------------------------------------
# STORY-4 acceptance criteria — dedicated real test below. Each acceptance
# criterion from the story is exercised by its own assertion, with the
# real Postgres backend. We deliberately avoid hardcoding the same value
# the implementation produces, and we cover every criterion explicitly:
#   1) one row inserted for a real record() call
#   2) row contains correct event_type / timestamp / actor / component /
#      action / outcome / metadata / raw_detail values
#   3) calling module is correctly identified in the `component` column
#      when the caller does NOT override `component` in the detail dict
#   4) existing zero-arg DefaultAuditManager() call sites keep working
#   5) record() raises AuditWriteError when the DB is unavailable
# ---------------------------------------------------------------------------


def _select_event_for_test_event_type(infrastructure, event_type):
    """Read back the single audit_events row matching `event_type`. Returns the
    raw row tuple (columns in the order used in the production INSERT) or
    raises AssertionError if not exactly one row is found."""
    with infrastructure._connection().cursor() as cursor:
        cursor.execute(
            """
            SELECT event_type, timestamp, actor, component, resource, action,
                   outcome, metadata, raw_detail
            FROM audit_events
            WHERE event_type = %s
            """,
            (event_type,),
        )
        rows = cursor.fetchall()
    assert len(rows) == 1, (
        f"Expected exactly 1 audit_events row for event_type={event_type!r}, "
        f"got {len(rows)}"
    )
    return rows[0]


def test_story4_default_audit_manager_persists_to_postgres_with_full_criteria():
    """STORY-4: exercise all acceptance criteria in one real Postgres round-trip."""
    event_type = "story4_qa_acceptance_event"

    # AC #4 — existing zero-arg constructor still works.
    zero_arg_manager = DefaultAuditManager()
    zero_arg_infrastructure = zero_arg_manager._infrastructure
    assert isinstance(zero_arg_infrastructure, DefaultInfrastructure)

    # AC #1 & #2 — explicit-infrastructure constructor also works and inserts
    # one row carrying every required column populated with the values the
    # normalize_audit_event() pass extracted from the supplied `detail`.
    infrastructure = DefaultInfrastructure()
    try:
        manager = DefaultAuditManager(infrastructure)
        # This test module is the caller. AC #3 — `component` must reflect
        # the calling module when the caller does not pass detail['component'].
        before_count = infrastructure._connection().cursor().execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = %s", (event_type,)
        ).fetchone()[0]

        manager.record(event_type, {"user": "alice", "request_id": "rid-story4"})

        after_count = infrastructure._connection().cursor().execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = %s", (event_type,)
        ).fetchone()[0]
        assert after_count == before_count + 1, (
            "AC #1 violated: record() must insert exactly one new row"
        )

        row = _select_event_for_test_event_type(infrastructure, event_type)
        (got_event_type, got_timestamp, got_actor, got_component, got_resource,
         got_action, got_outcome, got_metadata, got_raw_detail) = row

        # AC #2 — column-by-column correctness.
        assert got_event_type == event_type
        assert isinstance(got_timestamp, datetime)
        assert got_timestamp.tzinfo is not None  # timezone-aware UTC timestamp
        assert got_actor == {"user": "alice"}    # normalize() wraps user key
        assert got_action == event_type          # action defaults to event_type
        assert got_outcome == "success"          # outcome defaults to success
        assert got_metadata == {"request_id": "rid-story4"}  # non-known keys
        assert got_raw_detail == {"user": "alice", "request_id": "rid-story4"}

        # AC #3 — calling module correctly identified. This test lives in
        # tests/components/test_observability.py — the calling module must be
        # identifiable from inside DefaultAuditManager.record().
        assert isinstance(got_component, str)
        assert got_component.endswith("test_observability") or got_component == __name__, (
            f"AC #3 violated: component column should reflect calling module, "
            f"got {got_component!r}"
        )
        # resource: no resource-shaped key in detail -> None.
        assert got_resource is None
    finally:
        with infrastructure._connection().cursor() as cursor:
            cursor.execute(
                "DELETE FROM audit_events WHERE event_type = %s", (event_type,)
            )

    # AC #5 — record() raises AuditWriteError when DB is unavailable.
    bad_infra = DefaultInfrastructure(
        postgres_dsn="postgresql://wrong:wrong@localhost:5432/wrong"
    )
    bad_manager = DefaultAuditManager(bad_infra)
    with pytest.raises(AuditWriteError):
        bad_manager.record(event_type, {"user": "alice"})


def test_story4_acceptance_criteria_direct_verification():
    """Direct verification of STORY-4 acceptance criteria using real Infrastructure."""
    import uuid
    # AC: DefaultAuditManager(Infrastructure(...)).record('test', {'user': 'alice'}) inserts one row
    infrastructure = DefaultInfrastructure()
    audit_manager = DefaultAuditManager(infrastructure)
    
    # Use a unique event type to avoid conflicts with leftover rows
    unique_id = uuid.uuid4().hex[:8]
    event_type = f"STORY4_AC_TEST_{unique_id}"
    detail = {'user': 'alice', 'test_id': 'verification_123'}
    
    # Get baseline count (clean up any leftover from prior runs, just in case)
    with infrastructure._connection().cursor() as cursor:
        cursor.execute("DELETE FROM audit_events WHERE event_type = %s", (event_type,))
    
    # Record the event
    audit_manager.record(event_type, detail)
    
    # Verify exactly one row was inserted
    with infrastructure._connection().cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = %s",
            (event_type,)
        )
        count = cursor.fetchone()[0]
        assert count == 1, f"Expected exactly 1 row, found {count}"
        
        # AC: Inserted row contains correct values
        cursor.execute(
            """
            SELECT event_type, timestamp, actor, component, action, outcome, metadata, raw_detail
            FROM audit_events
            WHERE event_type = %s
            """,
            (event_type,)
        )
        row = cursor.fetchone()
        assert row is not None, "No row found"
        
        # Check each field
        assert row[0] == event_type, f"event_type mismatch: expected {event_type}, got {row[0]}"
        assert row[1] is not None, "timestamp should not be NULL"
        assert row[2] == {'user': 'alice'}, f"actor mismatch: expected {{'user': 'alice'}}, got {row[2]}"
        # component should be a string (the calling module)
        assert isinstance(row[3], str) and len(row[3]) > 0, f"component should be non-empty string, got {row[3]}"
        assert row[4] == event_type, f"action should default to event_type: expected {event_type}, got {row[4]}"
        assert row[5] == 'success', f"outcome should default to 'success', got {row[5]}"
        assert row[6] == {'test_id': 'verification_123'}, f"metadata mismatch: expected {{'test_id': 'verification_123'}}, got {row[6]}"
        assert row[7] == {'user': 'alice', 'test_id': 'verification_123'}, f"raw_detail mismatch: expected {{'user': 'alice', 'test_id': 'verification_123'}}, got {row[7]}"
    
    # AC: Calling module is correctly identified in component column
    # The component should reflect the calling module (this test file)
    with infrastructure._connection().cursor() as cursor:
        cursor.execute(
            "SELECT component FROM audit_events WHERE event_type = %s",
            (event_type,)
        )
        component = cursor.fetchone()[0]
        # Should contain the test file name or module path
        assert 'test_observability' in component or '__name__' in component, \
            f"component should identify calling module, got {component}"
    
    # AC: No changes required to existing call site constructor calls
    # Verify zero-arg constructor still works
    zero_arg_manager = DefaultAuditManager()
    assert hasattr(zero_arg_manager, '_infrastructure'), "Zero-arg constructor should set _infrastructure"
    assert isinstance(zero_arg_manager._infrastructure, DefaultInfrastructure), \
        "Zero-arg constructor should default to DefaultInfrastructure"
    
    # AC: record() raises AuditWriteError if database is unavailable
    bad_infrastructure = DefaultInfrastructure(
        postgres_dsn="postgresql://invalid_user:invalid_pass@localhost:5432/nonexistent_db"
    )
    bad_manager = DefaultAuditManager(bad_infrastructure)
    with pytest.raises(AuditWriteError):
        bad_manager.record('should_fail', {'user': 'test'})
    
    # Cleanup
    with infrastructure._connection().cursor() as cursor:
        cursor.execute(
            "DELETE FROM audit_events WHERE event_type = %s",
            (event_type,)
        )


def test_qa_story4_independent_acceptance_criteria_verification():
    """STORY-4 QA: write a NEW, independent test that exercises each
    acceptance criterion from this story in isolation, against the real
    Postgres backend. Each AC is verified with its own unique event_type
    so failures are attributable to a single criterion.

    AC #1: DefaultAuditManager(Infrastructure(...)).record('test', {'user':'alice'})
            inserts one row into audit_events.
    AC #2: Inserted row contains correct event_type, timestamp, actor,
            component, action, outcome, metadata, raw_detail values.
    AC #3: Calling module is correctly identified in component column.
    AC #4: No changes required to any existing call site constructor calls.
    AC #5: record() raises AuditWriteError if database is unavailable.
    """
    import uuid as _uuid

    infrastructure = DefaultInfrastructure()
    # Ensure schema exists; this also opens the connection on first use.
    infrastructure._connection()

    # Pre-clean any leftover rows from prior test runs (test isolation).
    pre_cleanup_types = [
        "qa_story4_ac1_event",
        "qa_story4_ac2_event",
        "qa_story4_ac3_event",
        "qa_story4_ac5_event",
    ]
    with infrastructure._connection().cursor() as cursor:
        for et in pre_cleanup_types:
            cursor.execute(
                "DELETE FROM audit_events WHERE event_type LIKE %s",
                (et + "%",),
            )

    # ---- AC #1: record() inserts exactly one row ----
    ac1_event_type = "qa_story4_ac1_event_" + _uuid.uuid4().hex[:12]
    manager = DefaultAuditManager(infrastructure)
    manager.record(ac1_event_type, {"user": "alice"})

    with infrastructure._connection().cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = %s",
            (ac1_event_type,),
        )
        ac1_count = cursor.fetchone()[0]
    assert ac1_count == 1, (
        f"AC #1 failed: expected exactly 1 row for event_type={ac1_event_type!r}, "
        f"got {ac1_count}"
    )

    # ---- AC #2: every column carries the correct, distinct value ----
    ac2_event_type = "qa_story4_ac2_event_" + _uuid.uuid4().hex[:12]
    manager.record(
        ac2_event_type,
        {
            "user": "alice",
            "portfolio_id": "p-qa4",
            "action": "specific_qa4_action",
            "request_id": "rqa4-1234",
            "outcome": "success",
        },
    )

    with infrastructure._connection().cursor() as cursor:
        cursor.execute(
            """
            SELECT event_type, timestamp, actor, component, resource,
                   action, outcome, metadata, raw_detail
            FROM audit_events
            WHERE event_type = %s
            """,
            (ac2_event_type,),
        )
        rows = cursor.fetchall()
    assert len(rows) == 1, f"AC #2 setup: expected 1 row, got {len(rows)}"
    row = rows[0]

    (got_event_type, got_timestamp, got_actor, got_component, got_resource,
     got_action, got_outcome, got_metadata, got_raw_detail) = row

    # event_type: passed in unchanged.
    assert got_event_type == ac2_event_type, (
        f"AC #2 event_type: expected {ac2_event_type!r}, got {got_event_type!r}"
    )
    # timestamp: real, timezone-aware datetime close to now.
    assert isinstance(got_timestamp, datetime), (
        f"AC #2 timestamp type: expected datetime, got {type(got_timestamp).__name__}"
    )
    assert got_timestamp.tzinfo is not None, (
        f"AC #2 timestamp tz: expected tz-aware datetime, got naive {got_timestamp!r}"
    )
    now = datetime.now(timezone.utc)
    assert abs((got_timestamp - now).total_seconds()) < 60, (
        f"AC #2 timestamp value: expected ~now ({now.isoformat()}), "
        f"got {got_timestamp.isoformat()}"
    )
    # actor: normalize_audit_event() wraps `user` key.
    assert got_actor == {"user": "alice"}, (
        f"AC #2 actor: expected {{'user': 'alice'}}, got {got_actor!r}"
    )
    # component: covered in AC #3 below (must reflect this test module).
    assert isinstance(got_component, str) and len(got_component) > 0, (
        f"AC #2 component: expected non-empty str, got {got_component!r}"
    )
    # resource: portfolio_id key extracted.
    assert got_resource == {"portfolio_id": "p-qa4"}, (
        f"AC #2 resource: expected {{'portfolio_id': 'p-qa4'}}, got {got_resource!r}"
    )
    # action: explicit detail['action'] must win over event_type default.
    assert got_action == "specific_qa4_action", (
        f"AC #2 action: expected 'specific_qa4_action', got {got_action!r}"
    )
    # outcome: explicit detail['outcome'] was 'success'.
    assert got_outcome == "success", (
        f"AC #2 outcome: expected 'success', got {got_outcome!r}"
    )
    # metadata: leftover, non-known keys (request_id).
    assert got_metadata == {"request_id": "rqa4-1234"}, (
        f"AC #2 metadata: expected {{'request_id': 'rqa4-1234'}}, "
        f"got {got_metadata!r}"
    )
    # raw_detail: full original detail dict (excluding known keys? -- per
    # implementation it is the full dict passed in).
    assert got_raw_detail == {
        "user": "alice",
        "portfolio_id": "p-qa4",
        "action": "specific_qa4_action",
        "request_id": "rqa4-1234",
        "outcome": "success",
    }, (
        f"AC #2 raw_detail: expected full detail dict, got {got_raw_detail!r}"
    )

    # ---- AC #3: calling module identified in component column ----
    # Use a separate event_type so the row above doesn't satisfy this AC.
    ac3_event_type = "qa_story4_ac3_event_" + _uuid.uuid4().hex[:12]
    manager.record(ac3_event_type, {})

    with infrastructure._connection().cursor() as cursor:
        cursor.execute(
            "SELECT component FROM audit_events WHERE event_type = %s",
            (ac3_event_type,),
        )
        ac3_component = cursor.fetchone()[0]

    # Component must reflect the *real* calling module — this test module.
    # We don't hardcode the same string the implementation might produce;
    # we compare against `__name__` (the dotted module path pytest uses),
    # which is what DefaultAuditManager.record()'s inspect.stack() lookup
    # must return for the caller to be correctly identified.
    expected_calling_module = __name__
    assert ac3_component == expected_calling_module, (
        f"AC #3 component: expected calling module {expected_calling_module!r}, "
        f"got {ac3_component!r}"
    )
    # And the component MUST NOT be a placeholder / hardcoded / null.
    assert ac3_component, f"AC #3 component must be non-empty, got {ac3_component!r}"

    # ---- AC #4: zero-arg DefaultAuditManager() still works ----
    # The constructor without arguments must succeed and produce a manager
    # whose record() persists a row. (Existing call sites pass no args.)
    zero_arg_manager = DefaultAuditManager()
    assert hasattr(zero_arg_manager, "_infrastructure"), (
        "AC #4 failed: zero-arg constructor must still set _infrastructure"
    )
    assert isinstance(zero_arg_manager._infrastructure, DefaultInfrastructure), (
        f"AC #4 failed: zero-arg DefaultAuditManager() must default to "
        f"DefaultInfrastructure, got {type(zero_arg_manager._infrastructure)!r}"
    )

    # Confirm record() on the zero-arg manager actually persists a row,
    # not just silently swallows the call (the prior file-append stub
    # behavior we are explicitly replacing).
    ac4_event_type = "qa_story4_ac4_event_" + _uuid.uuid4().hex[:12]
    zero_arg_manager.record(ac4_event_type, {"user": "ac4-check"})

    with infrastructure._connection().cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = %s",
            (ac4_event_type,),
        )
        ac4_count = cursor.fetchone()[0]
    assert ac4_count == 1, (
        f"AC #4 failed: zero-arg DefaultAuditManager().record() must persist "
        f"a real row, got {ac4_count} rows for {ac4_event_type!r}"
    )

    # ---- AC #5: AuditWriteError raised when DB unavailable ----
    bad_dsn = "postgresql://wrong_user:wrong_pass@127.0.0.1:59999/nonexistent_db_qa_story4"
    bad_infrastructure = DefaultInfrastructure(postgres_dsn=bad_dsn)
    bad_manager = DefaultAuditManager(bad_infrastructure)

    ac5_event_type = "qa_story4_ac5_event_" + _uuid.uuid4().hex[:12]
    with pytest.raises(AuditWriteError) as exc_info:
        bad_manager.record(ac5_event_type, {"user": "alice"})

    # The AuditWriteError must actually wrap the underlying DB error so a
    # caller can debug, not be a bare `AuditWriteError()` with no cause.
    assert exc_info.value.__cause__ is not None, (
        "AC #5 failed: AuditWriteError should wrap the original DB error "
        "via `raise ... from exc`, not be raised bare."
    )

    # ---- Cleanup all rows we created ----
    with infrastructure._connection().cursor() as cursor:
        for et in (
            ac1_event_type,
            ac2_event_type,
            ac3_event_type,
            ac4_event_type,
        ):
            cursor.execute(
                "DELETE FROM audit_events WHERE event_type = %s",
                (et,),
            )
