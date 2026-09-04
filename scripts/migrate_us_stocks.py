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

# Real identity for this migration in migration_log -- issue #74's
# verification script and issue #75's idempotency/failure tests both key
# off this exact name, so it isn't just documentation, it's a contract.
MIGRATION_NAME = "us_stock_portfolio_defaults_v1"

_LOG_INSERT_SQL = """
INSERT INTO migration_log (migration_name, run_at, status, rows_affected, error_message, dry_run)
VALUES (%s, now(), %s, %s, %s, %s)
"""


def _log(cursor, status: str, rows_affected: "int | None", error_message: "str | None", dry_run: bool) -> None:
    cursor.execute(_LOG_INSERT_SQL, (MIGRATION_NAME, status, rows_affected, error_message, dry_run))


def migrate_us_stocks(
    dsn: str = DEFAULT_POSTGRES_DSN,
    dry_run: bool | None = None,
) -> int:
    """Apply the US-stock normalization UPDATE.

    Returns rows_updated. When dry_run is True, no UPDATE is issued and
    the count reflects rows that would have changed. Every real
    invocation -- dry-run, successful real run, or a real failure -- logs
    exactly one row to migration_log (issue #71): this was a real gap
    found live -- the table existed with nothing ever writing to it, so
    issue #74's own verification script could never actually pass. Each
    connection here is autocommit (one implicit transaction per
    statement), so a failed real UPDATE genuinely rolls back on its own
    before the exception is caught and logged -- no explicit BEGIN/
    ROLLBACK needed for this single-statement migration.
    """
    if dry_run is None:
        dry_run = _is_dry_run()

    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            try:
                if dry_run:
                    cursor.execute(_COUNT_SQL)
                    rows = int(cursor.fetchone()[0])
                    _log(cursor, "DRY_RUN", rows, None, True)
                    return rows
                cursor.execute(_SQL)
                rows = cursor.rowcount
                _log(cursor, "SUCCESS", rows, None, False)
                return rows
            except Exception as exc:
                _log(cursor, "FAILED", None, str(exc), dry_run)
                raise


if __name__ == "__main__":
    rows = migrate_us_stocks()
    mode = "DRY-RUN" if _is_dry_run() else "APPLIED"
    print(f"{mode}: rows_updated={rows}")