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
def test_load_us_tickers_and_count_us_stocks(tmp_path, infra):
    """Test loading US tickers from a CSV and counting US stocks."""
    # Create a temporary CSV file with test data
    csv_content = """AAPL
MSFT
GOOGL
BRK.B
BRK-B
TSLA
AAPL  # duplicate
"""
    csv_file = tmp_path / "us_tickers.csv"
    csv_file.write_text(csv_content)
    
    # Load the tickers
    infra.load_us_tickers(str(csv_file))
    
    # Insert some test holdings data
    holdings_table = "holdings"
    # Insert a holding with a US ticker (should be counted)
    infra.store(holdings_table, {
        "id": "holding1",
        "security_id": "AAPL",
        "portfolio_id": "portfolio1"
    })
    # Insert a holding with another US ticker (should be counted)
    infra.store(holdings_table, {
        "id": "holding2",
        "security_id": "MSFT",
        "portfolio_id": "portfolio1"
    })
    # Insert a holding with a non-US ticker (should NOT be counted - has dot)
    infra.store(holdings_table, {
        "id": "holding3",
        "security_id": "BRK.B",
        "portfolio_id": "portfolio1"
    })
    # Insert a holding with a non-US ticker (should NOT be counted - has hyphen)
    infra.store(holdings_table, {
        "id": "holding4",
        "security_id": "BRK-B",
        "portfolio_id": "portfolio1"
    })
    # Insert a holding with a ticker not in CSV (should NOT be counted)
    infra.store(holdings_table, {
        "id": "holding5",
        "security_id": "XYZ",
        "portfolio_id": "portfolio1"
    })
    # Insert a holding for a different portfolio (should be counted in total but not portfolio-specific)
    infra.store(holdings_table, {
        "id": "holding6",
        "security_id": "GOOGL",
        "portfolio_id": "portfolio2"
    })
    
    # Count US stocks for portfolio1 (should be 2: AAPL and MSFT)
    count_portfolio1 = infra.count_us_stocks("portfolio1")
    assert count_portfolio1 == 2
    
    # Count US stocks for all portfolios (should be 3: AAPL, MSFT, GOOGL)
    count_all = infra.count_us_stocks()
    assert count_all == 3


@requires_postgres
def test_load_us_tickers_file_not_found(infra):
    """Test that load_us_tickers handles missing CSV file gracefully."""
    # This should not raise an exception
    infra.load_us_tickers("/nonexistent/path/us_tickers.csv")
    # Count should be 0 since table is empty
    count = infra.count_us_stocks()
    assert count == 0


@requires_postgres
def test_load_us_tickers_duplicates_and_empty_lines(tmp_path, infra):
    """Test that duplicates are deduplicated and empty lines are skipped."""
    # Create a temporary CSV file with duplicates, empty lines, and whitespace
    csv_content = """AAPL

MSFT
AAPL
  TSLA  
MSFT
"""
    csv_file = tmp_path / "us_tickers.csv"
    csv_file.write_text(csv_content)
    
    # Load the tickers
    infra.load_us_tickers(str(csv_file))
    
    # Insert holdings for each unique ticker
    infra.store("holdings", {"id": "h1", "security_id": "AAPL", "portfolio_id": "p1"})
    infra.store("holdings", {"id": "h2", "security_id": "MSFT", "portfolio_id": "p1"})
    infra.store("holdings", {"id": "h3", "security_id": "TSLA", "portfolio_id": "p1"})
    infra.store("holdings", {"id": "h4", "security_id": "XYZ", "portfolio_id": "p1"})  # not in CSV
    
    # Should count 3 US stocks (duplicates removed)
    count = infra.count_us_stocks("p1")
    assert count == 3


@requires_postgres
def test_load_us_tickers_truncates_and_reloads(tmp_path, infra):
    """Test that load_us_tickers truncates the temporary table and reloads the CSV each time."""
    # Create a temporary CSV file with initial data
    csv_content = """AAPL
MSFT
"""
    csv_file = tmp_path / "us_tickers.csv"
    csv_file.write_text(csv_content)

    # Load the tickers for the first time
    infra.load_us_tickers(str(csv_file))

    # Check that the temporary table has the two tickers
    with infra._connection().cursor() as cursor:
        cursor.execute("SELECT ticker FROM tmp_us_tickers ORDER BY ticker")
        rows = cursor.fetchall()
        assert [r[0] for r in rows] == ["AAPL", "MSFT"]

    # Now, without changing the CSV, load again (should truncate and reload the same)
    infra.load_us_tickers(str(csv_file))

    # Check again: still two
    with infra._connection().cursor() as cursor:
        cursor.execute("SELECT ticker FROM tmp_us_tickers ORDER BY ticker")
        rows = cursor.fetchall()
        assert [r[0] for r in rows] == ["AAPL", "MSFT"]

    # Now, insert an extra ticker directly into the temporary table
    with infra._connection().cursor() as cursor:
        cursor.execute("INSERT INTO tmp_us_tickers (ticker) VALUES (%s)", ("GOOGL",))

    # Check that we now have three
    with infra._connection().cursor() as cursor:
        cursor.execute("SELECT ticker FROM tmp_us_tickers ORDER BY ticker")
        rows = cursor.fetchall()
        assert set([r[0] for r in rows]) == {"AAPL", "MSFT", "GOOGL"}

    # Load the CSV again (should truncate and reload, removing the direct insert)
    infra.load_us_tickers(str(csv_file))

    # Check that we are back to two
    with infra._connection().cursor() as cursor:
        cursor.execute("SELECT ticker FROM tmp_us_tickers ORDER BY ticker")
        rows = cursor.fetchall()
        assert [r[0] for r in rows] == ["AAPL", "MSFT"]