"""Tests for DefaultInfrastructure (src/infrastructure_postgres.py).

These need a live Postgres and/or Redis (docker-compose.yml's services)
to actually run. _postgres_available()/_redis_available() probe with a
short timeout and the tests skip cleanly, with a clear reason, when
those services aren't up — they never fake a pass. Run `docker-compose
up -d` and re-run pytest to get real coverage from these.
"""

import uuid

import pytest

from infrastructure_postgres import (
    DEFAULT_POSTGRES_DSN,
    DEFAULT_REDIS_URL,
    DefaultInfrastructure,
)


def _postgres_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=1):
            return True
    except Exception:
        return False


def _redis_available() -> bool:
    try:
        import redis

        client = redis.Redis.from_url(DEFAULT_REDIS_URL, socket_connect_timeout=1)
        return client.ping()
    except Exception:
        return False


POSTGRES_SKIP_REASON = "no live Postgres reachable at DEFAULT_POSTGRES_DSN — run `docker-compose up -d` for real coverage"
REDIS_SKIP_REASON = "no live Redis reachable at DEFAULT_REDIS_URL — run `docker-compose up -d` for real coverage"

requires_postgres = pytest.mark.skipif(not _postgres_available(), reason=POSTGRES_SKIP_REASON)
requires_redis = pytest.mark.skipif(not _redis_available(), reason=REDIS_SKIP_REASON)


@pytest.fixture
def infra():
    return DefaultInfrastructure()


@requires_postgres
def test_store_then_retrieve_round_trips(infra):
    table = f"test_records_{uuid.uuid4().hex}"
    record = {"id": "widget-1", "name": "Widget", "count": 3}

    record_id = infra.store(table, record)
    retrieved = infra.retrieve(table, record_id)

    assert record_id == "widget-1"
    assert retrieved == record


@requires_postgres
def test_retrieve_missing_id_returns_none(infra):
    table = f"test_records_{uuid.uuid4().hex}"

    assert infra.retrieve(table, "does-not-exist") is None


@requires_postgres
def test_store_without_id_generates_one(infra):
    table = f"test_records_{uuid.uuid4().hex}"

    record_id = infra.store(table, {"name": "no id given"})

    assert record_id
    assert infra.retrieve(table, record_id) == {"name": "no id given"}


@requires_postgres
def test_query_with_jsonb_filter_returns_matching_subset(infra):
    table = f"test_records_{uuid.uuid4().hex}"
    infra.store(table, {"id": "a", "kind": "fruit", "name": "apple"})
    infra.store(table, {"id": "b", "kind": "fruit", "name": "banana"})
    infra.store(table, {"id": "c", "kind": "vegetable", "name": "carrot"})

    fruits = infra.query(table, {"kind": "fruit"})

    assert {record["name"] for record in fruits} == {"apple", "banana"}


@requires_postgres
def test_publish_then_subscribe_delivers_and_marks_consumed(infra):
    topic = f"test_topic_{uuid.uuid4().hex}"
    infra.publish(topic, {"message": "first"})
    infra.publish(topic, {"message": "second"})

    delivered = []
    infra.subscribe(topic, delivered.append)

    assert delivered == [{"message": "first"}, {"message": "second"}]

    redelivered = []
    infra.subscribe(topic, redelivered.append)
    assert redelivered == []  # already-consumed events aren't redelivered


@requires_postgres
def test_schedule_returns_an_id(infra):
    schedule_id = infra.schedule(60.0, {"job": "send_reminder"})

    assert schedule_id


@requires_redis
def test_cache_set_then_cache_get_round_trips(infra):
    key = f"test_cache_{uuid.uuid4().hex}"

    infra.cache_set(key, {"nested": ["value", 1, True]}, ttl_seconds=30)

    assert infra.cache_get(key) == {"nested": ["value", 1, True]}


@requires_redis
def test_cache_get_missing_key_returns_none(infra):
    assert infra.cache_get(f"test_cache_missing_{uuid.uuid4().hex}") is None


def test_get_secret_reads_environment_variable(monkeypatch, infra):
    monkeypatch.setenv("PORTFOLIO_AGENT_TEST_SECRET", "shh")

    assert infra.get_secret("PORTFOLIO_AGENT_TEST_SECRET") == "shh"


def test_get_secret_raises_key_error_when_unset(monkeypatch, infra):
    monkeypatch.delenv("PORTFOLIO_AGENT_TEST_SECRET", raising=False)

    with pytest.raises(KeyError):
        infra.get_secret("PORTFOLIO_AGENT_TEST_SECRET")


def test_constructing_default_infrastructure_does_not_touch_network():
    """No connection attempt happens at construction time — only when
    a method that needs it is actually called."""
    infra = DefaultInfrastructure(
        postgres_dsn="postgresql://unreachable-host:5432/nope",
        redis_url="redis://unreachable-host:6379/0",
    )

    assert infra is not None


@requires_postgres
def test_migration_log_table_has_required_columns_and_types(infra):
    """STORY-1: the migration_log table exists with all required columns
    and types after _ensure_schema runs (triggered lazily by a method call)."""
    import psycopg
    # Touching any Postgres method triggers _ensure_schema; we just need
    # a side-effect-bearing call, store() is the simplest.
    infra.store(f"test_migration_log_probe_{uuid.uuid4().hex}", {"id": "probe", "x": 1})

    expected_columns = {
        "id": "bigint",
        "migration_name": "character varying",
        "run_at": "timestamp with time zone",
        "status": "character varying",
        "rows_affected": "bigint",
        "error_message": "text",
        "dry_run": "boolean",
    }

    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn, conn.cursor() as cursor:
        # 1. table exists with the expected columns and types
        cursor.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'migration_log'
            ORDER BY column_name
            """
        )
        actual = dict(cursor.fetchall())

    assert actual == expected_columns, (
        f"migration_log columns mismatch.\n"
        f"  expected: {expected_columns}\n"
        f"  actual:   {actual}"
    )


@requires_postgres
def test_migration_log_index_exists_on_migration_name_and_run_at(infra):
    """STORY-1: index idx_migration_log_name_run exists on
    (migration_name, run_at)."""
    import psycopg
    infra.store(f"test_migration_log_idx_{uuid.uuid4().hex}", {"id": "probe", "x": 1})

    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = 'migration_log'
            """
        )
        rows = cursor.fetchall()

    names = {row[0] for row in rows}
    defs = {row[1] for row in rows}
    assert "idx_migration_log_name_run" in names, (
        f"idx_migration_log_name_run not found; pg_indexes returned: {rows}"
    )
    # Confirm it covers exactly the required columns in the required order
    matching = [d for d in defs if "idx_migration_log_name_run" in d]
    assert any(
        "(migration_name, run_at)" in d.lower() for d in matching
    ), f"index definition does not match required columns: {matching}"


@requires_postgres
def test_ensure_schema_is_idempotent_for_migration_log(infra):
    """STORY-1: running _ensure_schema twice does not raise and leaves
    the migration_log table usable. Insert a sentinel row after the
    first run, re-trigger schema creation, and confirm the row is
    still there."""
    import psycopg
    sentinel_id = f"idem-{uuid.uuid4().hex}"

    # First run + insert a sentinel row.
    infra.store(f"test_migration_log_idem_{uuid.uuid4().hex}", {"id": "p", "x": 1})
    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO migration_log
                (migration_name, run_at, status, rows_affected, error_message, dry_run)
            VALUES (%s, now(), %s, %s, %s, %s)
            RETURNING id
            """,
            ("sentinel_migration", "ok", 7, None, False),
        )
        sentinel_pk = cursor.fetchone()[0]

    # Second run: call _ensure_schema again on the same connection.
    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn:
        DefaultInfrastructure._ensure_schema(conn)  # must not raise

    # Sentinel row must still be there with the same id and rows_affected.
    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT rows_affected, status FROM migration_log WHERE id = %s",
            (sentinel_pk,),
        )
        row = cursor.fetchone()

    assert row is not None, "sentinel row was lost after re-running _ensure_schema"
    assert row == (7, "ok"), f"sentinel row data altered: {row}"


@requires_postgres
def test_ensure_schema_does_not_alter_existing_tables(infra):
    """STORY-1: pre-existing rows in the records / queue_events /
    scheduled_tasks tables are not altered or lost when _ensure_schema
    runs (which is what happens every time a new DefaultInfrastructure
    opens its first connection)."""
    import psycopg
    table = f"test_migration_log_existing_{uuid.uuid4().hex}"

    # Seed each of the three pre-existing tables through the public API.
    infra.store(table, {"id": "rec-1", "name": "alpha"})
    infra.publish(table, {"event": "beta"})
    schedule_id = infra.schedule(3600.0, {"job": "gamma"})

    # Drop the in-memory connection so the next call re-triggers _ensure_schema.
    infra._pg_connection = None

    # Trigger another schema run; pre-existing rows must survive.
    infra.store(table, {"id": "rec-2", "name": "alpha2"})

    with psycopg.connect(DEFAULT_POSTGRES_DSN) as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT data FROM records WHERE table_name = %s AND id = %s",
            (table, "rec-1"),
        )
        rec_row = cursor.fetchone()
        assert rec_row is not None and rec_row[0]["name"] == "alpha", (
            f"records row altered/lost: {rec_row}"
        )

        cursor.execute(
            "SELECT event FROM queue_events WHERE topic = %s",
            (table,),
        )
        evt_row = cursor.fetchone()
        assert evt_row is not None and evt_row[0]["event"] == "beta", (
            f"queue_events row altered/lost: {evt_row}"
        )

        cursor.execute(
            "SELECT task FROM scheduled_tasks WHERE id = %s",
            (int(schedule_id),),
        )
        sched_row = cursor.fetchone()
        assert sched_row is not None and sched_row[0]["job"] == "gamma", (
            f"scheduled_tasks row altered/lost: {sched_row}"
        )
