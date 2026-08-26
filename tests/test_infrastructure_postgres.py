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
