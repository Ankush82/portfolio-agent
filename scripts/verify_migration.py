"""Thin wrapper around scripts/verify_migration.sql.

Story STORY-4 (issue #74): ops/QA script for confirming the US-stock
normalization migration applied correctly. Mirrors
scripts/migrate_us_stocks.py's structure (read SQL file, connect,
execute, summarize) but is strictly SELECT-only -- this script never
modifies the stocks table or migration_log.

Exit codes
----------
  0  All checks pass:
       (a) every row in `stocks` has currency='USD', exchange IS NULL,
           and symbol_suffix IS NULL;
       (b) migration_log has at least one row with
           migration_name='us_stock_portfolio_defaults_v1' and
           status='SUCCESS'.
  1  At least one check failed. Summary counts are always printed so
     ops/QA can see exactly which one.

Summary counts printed regardless of pass/fail:
  * total rows in stocks
  * bad currency count (currency <> 'USD')
  * bad exchange count (exchange IS NOT NULL)
  * bad suffix count (symbol_suffix IS NOT NULL)
  * log success flag (1 if a SUCCESS row exists for the migration
    name, 0 otherwise)
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN
from scripts.migrate_us_stocks import MIGRATION_NAME

_SQL_PATH = Path(__file__).with_name("verify_migration.sql")
_SQL = _SQL_PATH.read_text()

# Exit codes documented in the module docstring.
EXIT_PASS = 0
EXIT_FAIL = 1


def _collect_summaries(connection: psycopg.Connection) -> dict:
    """Run verify_migration.sql and return both result sets as a dict.

    Returns:
        {
          "stocks":      {total_rows, bad_currency, bad_exchange, bad_suffix},
          "migration_log": {log_total, log_success, log_failed, log_dry_run,
                           last_status, last_run_at},
        }
    """
    with connection.cursor() as cursor:
        cursor.execute(_SQL)
        stocks_row = cursor.fetchone()
        stocks_cols = [d.name for d in cursor.description]

        # Fetch the second result set -- psycopg exposes this via
        # nextset() when multiple statements were sent in one execute.
        cursor.nextset()
        log_row = cursor.fetchone()
        log_cols = [d.name for d in cursor.description]

    if stocks_row is None or log_row is None:
        raise RuntimeError(
            "verify_migration.sql returned fewer than two result sets; "
            "the SQL file appears to be malformed"
        )

    return {
        "stocks": dict(zip(stocks_cols, stocks_row)),
        "migration_log": dict(zip(log_cols, log_row)),
    }


def _evaluate(stocks_summary: dict, log_summary: dict) -> tuple[bool, list[str]]:
    """Return (passed, list_of_failure_reasons).

    A failure reason is a human-readable string identifying which check
    failed. Empty list means all checks passed.
    """
    failures: list[str] = []

    if stocks_summary["total_rows"] != (
        stocks_summary["bad_currency"]
        + stocks_summary["bad_exchange"]
        + stocks_summary["bad_suffix"]
    ):
        # If everything's bad, the migration never ran. If the table is
        # empty (total_rows == 0), bad_* are all 0 and the equality
        # holds trivially -- an empty stocks table is vacuously
        # normalized, so we still pass on it.
        pass
    # The real per-column check: any nonzero bad_* count is a failure.
    if stocks_summary["bad_currency"] != 0:
        failures.append(
            f"{stocks_summary['bad_currency']} stock row(s) have currency <> 'USD'"
        )
    if stocks_summary["bad_exchange"] != 0:
        failures.append(
            f"{stocks_summary['bad_exchange']} stock row(s) have exchange IS NOT NULL"
        )
    if stocks_summary["bad_suffix"] != 0:
        failures.append(
            f"{stocks_summary['bad_suffix']} stock row(s) have symbol_suffix IS NOT NULL"
        )

    if log_summary["log_success"] == 0:
        failures.append(
            f"migration_log has no SUCCESS row for migration_name={MIGRATION_NAME!r}"
        )

    return (len(failures) == 0, failures)


def verify_migration(dsn: str = DEFAULT_POSTGRES_DSN) -> int:
    """Run the verification and return the exit code (0 == pass).

    Always prints summary counts to stdout. Writes failure reasons to
    stderr when something fails so ops/QA can see both at a glance.
    """
    with psycopg.connect(dsn, autocommit=True) as connection:
        summaries = _collect_summaries(connection)

    stocks = summaries["stocks"]
    log = summaries["migration_log"]

    passed, failures = _evaluate(stocks, log)
    log_success_flag = 1 if log["log_success"] > 0 else 0

    print(f"stocks.total_rows     = {stocks['total_rows']}")
    print(f"stocks.bad_currency   = {stocks['bad_currency']}")
    print(f"stocks.bad_exchange   = {stocks['bad_exchange']}")
    print(f"stocks.bad_suffix     = {stocks['bad_suffix']}")
    print(f"migration_log.success = {log_success_flag}")
    if not passed:
        print("", file=sys.stderr)
        for reason in failures:
            print(f"FAIL: {reason}", file=sys.stderr)

    return EXIT_PASS if passed else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(verify_migration())
