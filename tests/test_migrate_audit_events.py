"""Tests for scripts/migrate_audit_events.sql (Story STORY-3).

Needs a live Postgres at DEFAULT_POSTGRES_DSN — same precondition as
the rest of the integration tests in this repo.
"""

from __future__ import annotations

import os
import pytest
import psycopg


# Use the same default DSN as infrastructure_postgres.py to match test env
DEFAULT_POSTGRES_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://portfolio_agent:portfolio_agent@localhost:5432/portfolio_agent"
)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=1):
            return True
    except psycopg.Error:
        return False


POSTGRES_SKIP_REASON = (
    "no live Postgres reachable at DEFAULT_POSTGRES_DSN — "
    "run `docker-compose up -d` for real coverage"
)
pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason=POSTGRES_SKIP_REASON
)


@pytest.fixture
def clean_audit_events():
    """Create audit_events table, run the migration, then drop on teardown."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Ensure schema_migrations exists (created by infrastructure_postgres.py)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    id BIGSERIAL PRIMARY KEY,
                    migration_name VARCHAR NOT NULL UNIQUE,
                    applied_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            # Run the audit_events migration
            cur.execute(open("scripts/migrate_audit_events.sql").read())
    yield
    # Teardown: drop the table
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS audit_events CASCADE")
            cur.execute("DELETE FROM schema_migrations WHERE migration_name = 'audit_events_v1'")


def test_migration_creates_table(clean_audit_events):
    """Verify the audit_events table exists with the correct structure."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_name = 'audit_events'
                ORDER BY ordinal_position
            """)
            columns = {row[0]: {"data_type": row[1], "nullable": row[2], "default": row[3]}
                       for row in cur.fetchall()}

    assert "id" in columns
    assert columns["id"]["data_type"] == "uuid"
    assert columns["id"]["nullable"] == "NO"
    assert "gen_random_uuid()" in (columns["id"]["default"] or "")

    assert "event_type" in columns
    assert columns["event_type"]["data_type"] == "character varying"
    assert columns["event_type"]["nullable"] == "NO"

    assert "timestamp" in columns
    assert columns["timestamp"]["data_type"] == "timestamp with time zone"
    assert columns["timestamp"]["nullable"] == "NO"

    assert "actor" in columns
    assert columns["actor"]["data_type"] == "jsonb"
    assert columns["actor"]["nullable"] == "NO"

    assert "component" in columns
    assert columns["component"]["data_type"] == "character varying"

    assert "resource" in columns
    assert columns["resource"]["data_type"] == "jsonb"

    assert "action" in columns
    assert columns["action"]["data_type"] == "character varying"

    assert "outcome" in columns
    assert columns["outcome"]["data_type"] == "character varying"
    assert "success" in (columns["outcome"]["default"] or "")

    assert "metadata" in columns
    assert columns["metadata"]["data_type"] == "jsonb"
    assert columns["metadata"]["nullable"] == "NO"

    assert "raw_detail" in columns
    assert columns["raw_detail"]["data_type"] == "jsonb"


def test_migration_creates_primary_key(clean_audit_events):
    """Verify the primary key constraint is enforced on the id column."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT constraint_name
                FROM information_schema.table_constraints
                WHERE table_name = 'audit_events'
                  AND constraint_type = 'PRIMARY KEY'
            """)
            pk = cur.fetchone()
            assert pk is not None, "No primary key found on audit_events"


def test_migration_creates_all_indexes(clean_audit_events):
    """Verify all 5 indexes are created."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE tablename = 'audit_events'
            """)
            indexes = {row[0]: row[1] for row in cur.fetchall()}

    expected_indexes = [
        "idx_audit_events_timestamp",
        "idx_audit_events_event_type",
        "idx_audit_events_actor",
        "idx_audit_events_component",
        "idx_audit_events_resource",
    ]
    for idx_name in expected_indexes:
        assert idx_name in indexes, f"Missing index: {idx_name}"

    # Verify timestamp is DESC
    assert "DESC" in indexes["idx_audit_events_timestamp"]

    # Verify GIN indexes for JSONB columns
    assert "USING gin" in indexes["idx_audit_events_actor"].lower()
    assert "USING gin" in indexes["idx_audit_events_resource"].lower()


def test_migration_is_idempotent(clean_audit_events):
    """Re-running the migration on an existing table does not error."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Re-run the migration — should not raise
            cur.execute(open("scripts/migrate_audit_events.sql").read())

    # Table still exists and has correct structure
    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit_events")
            count = cur.fetchone()[0]
            assert count == 0  # No rows yet


def test_migration_insert_and_select(clean_audit_events):
    """Verify the table accepts inserts and can be queried."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Insert a sample audit event
            cur.execute("""
                INSERT INTO audit_events (event_type, actor, component, action, outcome, metadata)
                VALUES (
                    'user.login',
                    '{"user_id": "123", "role": "admin"}',
                    'auth_service',
                    'login',
                    'success',
                    '{"ip": "192.168.1.1"}'
                )
                RETURNING id, event_type, timestamp
            """)
            row = cur.fetchone()
            assert row is not None
            assert row[0] is not None  # UUID generated
            assert row[1] == "user.login"
            assert row[2] is not None  # timestamp generated

    # Verify data persists
    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT event_type, actor, component FROM audit_events WHERE event_type = 'user.login'")
            row = cur.fetchone()
            assert row is not None
            assert row[2] == "auth_service"


def test_migration_records_in_schema_migrations(clean_audit_events):
    """Verify the migration is recorded in schema_migrations."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT migration_name, applied_at
                FROM schema_migrations
                WHERE migration_name = 'audit_events_v1'
            """)
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "audit_events_v1"
            assert row[1] is not None


# ─── STORY-3 QA: Comprehensive acceptance-criteria test ─────────────────────
def test_story3_acceptance_criteria_all(clean_audit_events):
    """Story-3 QA: verify ALL acceptance criteria in one test.

    Criteria:
    1. Migration runs successfully on a clean database and creates audit_events table
    2. Table has all columns specified with correct types and defaults
    3. All 5 indexes are created
    4. Re-running migration on existing table does not error (idempotent)
    5. Primary key constraint enforced on id column
    """
    # Criterion 1: table created
    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT FROM pg_tables WHERE tablename = 'audit_events')"
            )
            assert cur.fetchone()[0], "audit_events table was not created"

    # Criterion 2: all columns with correct types and defaults
    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_name = 'audit_events'
                ORDER BY ordinal_position
            """)
            cols = {r[0]: {"type": r[1], "nullable": r[2], "default": r[3]}
                    for r in cur.fetchall()}

    # Exact column list
    assert set(cols.keys()) == {
        "id", "event_type", "timestamp", "actor", "component",
        "resource", "action", "outcome", "metadata", "raw_detail",
    }, f"Unexpected columns: {set(cols.keys())}"

    # Types
    assert cols["id"]["type"] == "uuid"
    assert cols["event_type"]["type"] == "character varying"
    assert cols["timestamp"]["type"] == "timestamp with time zone"
    assert cols["actor"]["type"] == "jsonb"
    assert cols["component"]["type"] == "character varying"
    assert cols["resource"]["type"] == "jsonb"
    assert cols["action"]["type"] == "character varying"
    assert cols["outcome"]["type"] == "character varying"
    assert cols["metadata"]["type"] == "jsonb"
    assert cols["raw_detail"]["type"] == "jsonb"

    # Nullability: event_type, timestamp, actor, metadata must be NOT NULL
    for col in ("event_type", "timestamp", "actor", "metadata"):
        assert cols[col]["nullable"] == "NO", f"{col} must be NOT NULL"

    # Defaults
    assert "gen_random_uuid()" in (cols["id"]["default"] or "")
    assert "now()" in (cols["timestamp"]["default"] or "").lower()
    assert "'{}'" in (cols["actor"]["default"] or "") or "{}" in (cols["actor"]["default"] or "")
    assert "success" in (cols["outcome"]["default"] or "").lower()
    assert "'{}'" in (cols["metadata"]["default"] or "") or "{}" in (cols["metadata"]["default"] or "")

    # Criterion 3: all 5 indexes created
    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT indexname, indexdef FROM pg_indexes
                WHERE tablename = 'audit_events'
            """)
            indexes = {r[0]: r[1] for r in cur.fetchall()}

    expected = [
        ("idx_audit_events_timestamp", "DESC", None),
        ("idx_audit_events_event_type", None, None),
        ("idx_audit_events_actor", None, "gin"),
        ("idx_audit_events_component", None, None),
        ("idx_audit_events_resource", None, "gin"),
    ]
    for name, ordering, idx_type in expected:
        assert name in indexes, f"Missing index: {name}"
        if ordering:
            assert ordering.upper() in indexes[name].upper(), \
                f"{name} must have {ordering} ordering"
        if idx_type:
            assert f"USING {idx_type}" in indexes[name].upper(), \
                f"{name} must be USING {idx_type.upper()}"

    # Criterion 4: idempotent — re-running migration does not error
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(open("scripts/migrate_audit_events.sql").read())
    # Table still exists after re-run
    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit_events")
            assert cur.fetchone()[0] == 0

    # Criterion 5: primary key enforced — duplicate UUIDs must be rejected
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO audit_events (id, event_type, actor, metadata)
                VALUES (
                    '00000000-0000-0000-0000-000000000001',
                    'test.event',
                    '{}',
                    '{}'
                )
            """)
            with pytest.raises(psycopg.errors.UniqueViolation, match="duplicate key"):
                cur.execute("""
                    INSERT INTO audit_events (id, event_type, actor, metadata)
                    VALUES (
                        '00000000-0000-0000-0000-000000000001',
                        'test.event2',
                        '{}',
                        '{}'
                    )
                """)
