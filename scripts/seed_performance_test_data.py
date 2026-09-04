"""Real bulk data generator for issue #77 (performance/resource validation).

Creates `stocks` + `tmp_us_tickers` if they don't already exist (the same
shape scripts/migrate_us_stocks.py's tests use -- see
tests/test_migrate_us_stocks.py's `stocks_table` fixture) and bulk-loads a
real, large synthetic dataset via `generate_series` + `INSERT ... SELECT`
entirely inside Postgres -- row-by-row inserts from Python would take
hours at 5-10 million rows; this takes seconds.

Usage: python3 scripts/seed_performance_test_data.py [row_count]
Default row_count is 5_000_000 (the story's own minimum). A fixed
fraction of rows are seeded as already-normalized (no-op targets) and
the rest as needing normalization, so a real run has real work to do
and idempotency is still observable on a second pass.
"""

from __future__ import annotations

import sys
import time

import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN

_MATCH_FRACTION = 0.3  # 30% of stocks rows are in tmp_us_tickers (need normalizing)
_ALREADY_CORRECT_FRACTION = 0.5  # of the matched rows, half are already normalized


def seed(dsn: str = DEFAULT_POSTGRES_DSN, row_count: int = 5_000_000) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS stocks (
                    id TEXT PRIMARY KEY,
                    currency TEXT,
                    exchange TEXT,
                    symbol_suffix TEXT
                )
                """
            )
            cur.execute("CREATE TABLE IF NOT EXISTS tmp_us_tickers (id TEXT PRIMARY KEY)")
            cur.execute("TRUNCATE stocks, tmp_us_tickers")

            start = time.monotonic()
            cur.execute(
                """
                INSERT INTO stocks (id, currency, exchange, symbol_suffix)
                SELECT
                    'PERF-' || i::text,
                    CASE
                        WHEN i %% 10 < 3 AND i %% 20 < 5 THEN 'USD'  -- already-correct subset
                        ELSE 'EUR'
                    END,
                    CASE WHEN i %% 10 < 3 AND i %% 20 < 5 THEN NULL ELSE 'XETRA' END,
                    CASE WHEN i %% 10 < 3 AND i %% 20 < 5 THEN NULL ELSE '.F' END
                FROM generate_series(1, %s) AS i
                """,
                (row_count,),
            )
            cur.execute(
                """
                INSERT INTO tmp_us_tickers (id)
                SELECT 'PERF-' || i::text
                FROM generate_series(1, %s) AS i
                WHERE i %% 10 < 3
                """,
                (row_count,),
            )
            elapsed = time.monotonic() - start

            cur.execute("SELECT COUNT(*) FROM stocks")
            stocks_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM tmp_us_tickers")
            matched_count = cur.fetchone()[0]

    print(
        f"Seeded {stocks_count:,} stocks rows ({matched_count:,} in tmp_us_tickers) "
        f"in {elapsed:.1f}s"
    )


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5_000_000
    seed(row_count=n)
