"""Tests for scripts/migrate_us_stocks.{sql,py} (Story STORY-3).

Needs a live Postgres at DEFAULT_POSTGRES_DSN — same precondition as
the rest of the integration tests in this repo.
"""

from __future__ import annotations

import pytest
import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN


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
def stocks_table():
    """Create the canonical `stocks` + `tmp_us_tickers` pair this story
    operates on, then drop them on teardown."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE stocks (
                    id TEXT PRIMARY KEY,
                    currency TEXT,
                    exchange TEXT,
                    symbol_suffix TEXT
                )
                """
            )
            cur.execute("CREATE TABLE tmp_us_tickers (id TEXT PRIMARY KEY)")

    yield "stocks", "tmp_us_tickers"

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS stocks")
            cur.execute("DROP TABLE IF EXISTS tmp_us_tickers")


def _seed(conn) -> list[str]:
    """Three target tickers + one non-target:
      * AAPL   — already correct (USD, exchange/suffix NULL)
      * MSFT   — wrong currency ('EUR') only
      * GOOGL  — exchange set, suffix set, currency 'USD' already
      * TSLA   — NOT in tmp_us_tickers; must never be touched.
    """
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO tmp_us_tickers (id) VALUES (%s)",
            [("AAPL",), ("MSFT",), ("GOOGL",)],
        )
        cur.executemany(
            "INSERT INTO stocks (id, currency, exchange, symbol_suffix) "
            "VALUES (%s, %s, %s, %s)",
            [
                ("AAPL", "USD", None, None),
                ("MSFT", "EUR", None, None),
                ("GOOGL", "USD", "NASDAQ", ".OLD"),
                ("TSLA", "EUR", "XETRA", ".F"),
            ],
        )
    return ["AAPL", "MSFT", "GOOGL"]


def _read(conn, ids: list[str]) -> dict[str, tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, currency, exchange, symbol_suffix FROM stocks "
            "WHERE id = ANY(%s)",
            (ids,),
        )
        return {row[0]: row[1:] for row in cur.fetchall()}


def test_migrate_us_stocks_dry_run_real_then_idempotent(stocks_table, monkeypatch):
    """Seeds rows, runs the migration in dry-run mode, then for real,
    then a second real run to prove idempotency. Single test function
    so all three phases share fixtures and one setup."""
    from scripts.migrate_us_stocks import migrate_us_stocks

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        target_ids = _seed(conn)
        before = _read(conn, target_ids + ["TSLA"])

    # Phase 1: dry-run — no UPDATE issued, but the count is non-zero.
    monkeypatch.setenv("MIGRATION_DRY_RUN", "true")
    dry_run_count = migrate_us_stocks()
    assert dry_run_count == 2, (
        f"dry-run should report the 2 rows that differ (MSFT, GOOGL); got {dry_run_count}"
    )

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        after_dry = _read(conn, target_ids + ["TSLA"])
    assert after_dry == before, "dry-run must not modify any rows"

    # Phase 2: real run — updates the 2 differing rows.
    monkeypatch.setenv("MIGRATION_DRY_RUN", "false")
    real_count = migrate_us_stocks()
    assert real_count == 2, f"real run should update 2 rows; got {real_count}"

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        after_real = _read(conn, target_ids + ["TSLA"])

    # MSFT and GOOGL are now normalized; AAPL was already correct.
    assert after_real["AAPL"] == ("USD", None, None)
    assert after_real["MSFT"] == ("USD", None, None)
    assert after_real["GOOGL"] == ("USD", None, None)
    # TSLA is not in tmp_us_tickers and must be untouched.
    assert after_real["TSLA"] == ("EUR", "XETRA", ".F")

    # Phase 3: idempotency — a second real run updates zero rows.
    second_count = migrate_us_stocks()
    assert second_count == 0, (
        f"second real run must be a no-op; got {second_count}"
    )