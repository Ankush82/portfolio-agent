"""Thin wrapper around scripts/migrate_us_stocks.sql.

Story STORY-3: applies (or dry-runs) the US-stock normalization UPDATE.

Gating is done in Python so the SQL file stays plain and reusable:
  * MIGRATION_DRY_RUN unset / falsy  -> run the real UPDATE, return its rowcount
  * MIGRATION_DRY_RUN truthy (case-insensitive true/1/yes) -> issue no UPDATE,
    return COUNT(*) of rows the UPDATE would have changed

The dry-run count mirrors the UPDATE's WHERE clause exactly, so the
two numbers agree on every input.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN

_SQL_PATH = Path(__file__).with_name("migrate_us_stocks.sql")
_SQL = _SQL_PATH.read_text()

_TRUTHY = {"true", "1", "yes"}


def _is_dry_run() -> bool:
    raw = os.environ.get("MIGRATION_DRY_RUN", "")
    return raw.strip().lower() in _TRUTHY


_COUNT_SQL = """
SELECT COUNT(*)
FROM stocks
JOIN tmp_us_tickers ON tmp_us_tickers.id = stocks.id
WHERE stocks.currency <> 'USD'
   OR stocks.exchange IS NOT NULL
   OR stocks.symbol_suffix IS NOT NULL
"""


def migrate_us_stocks(
    dsn: str = DEFAULT_POSTGRES_DSN,
    dry_run: bool | None = None,
) -> int:
    """Apply the US-stock normalization UPDATE.

    Returns rows_updated. When dry_run is True, no UPDATE is issued and
    the count reflects rows that would have changed.
    """
    if dry_run is None:
        dry_run = _is_dry_run()

    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            if dry_run:
                cursor.execute(_COUNT_SQL)
                return int(cursor.fetchone()[0])
            cursor.execute(_SQL)
            return cursor.rowcount


if __name__ == "__main__":
    rows = migrate_us_stocks()
    mode = "DRY-RUN" if _is_dry_run() else "APPLIED"
    print(f"{mode}: rows_updated={rows}")