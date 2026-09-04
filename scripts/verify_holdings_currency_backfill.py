"""Ops/QA verification for scripts/backfill_holdings_currency.py.

Story STORY-2 (issue #46): ops/QA script for confirming the holdings
currency backfill applied correctly. Mirrors scripts/verify_migration.py's
structure on purpose (read SQL, connect, execute, summarize, decide
pass/fail, return exit code) but checks the backfill's real targets
instead of the us_stock migration's:

  * No row in `records` (DefaultInfrastructure's real storage, with
    table_name='holdings') may have data->>'currency' IS NULL. The
    backfill uses jsonb_set(data, '{currency}', 'USD') on those rows,
    so post-backfill the NULL case should be impossible.
  * `schema_migrations` must contain exactly one row keyed on the
    backfill's MIGRATION_NAME. (The backfill uses ON CONFLICT DO NOTHING
    on the UNIQUE(migration_name) constraint, so this row is created
    exactly once across all successful real runs.)
  * `migration_log` must contain at least one row with MIGRATION_NAME and
    status='SUCCESS' (so an operator can see when it last succeeded,
    and a dry-run-only invocation doesn't pass verification).

Exit codes
----------
  0  All checks pass.
  1  At least one check failed. Summary counts are always printed so
     ops/QA can see exactly which one.

This script is strictly SELECT-only -- it never modifies records,
migration_log, or schema_migrations. Like scripts/verify_migration.py,
it's invoked from CI / ops as a smoke test after a backfill run.

The SQL lives inline (in a constant) rather than in a separate .sql
file because the backfill script itself is single-language Python with
no companion .sql (unlike migrate_us_stocks.py, which has a sibling
.sql invoked by psql). Keeping verify_holdings_currency_backfill.py
single-file mirrors that shape and avoids inventing a verify_*.sql file
that nothing else would consume.
"""

from __future__ import annotations

import sys

import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN
from scripts.backfill_holdings_currency import MIGRATION_NAME

# Two result sets, executed in a single .execute() call:
#   (1) holdings summary -- counts of normalized vs un-normalized rows.
#   (2) schema_migrations summary -- is the backfill row present?
#   (3) migration_log summary -- has the backfill ever succeeded?
#
# Same shape as scripts/verify_migration.sql's two-result-set layout,
# extended with a third result set for schema_migrations because that
# table didn't exist when STORY-4 (issue #74) was written.
_VERIFY_SQL = f"""
-- (1) Holdings summary: real storage is `records` (DefaultInfrastructure),
-- so the verification reads records.data->>'currency' rather than a
-- top-level `currency` column on a `holdings` table. total_rows is the
-- count of holdings rows we know about; bad_currency is the count whose
-- data JSONB still has a NULL currency key (the failure mode the
-- backfill is supposed to fix).
SELECT
    COUNT(*)::bigint                                              AS total_rows,
    COUNT(*) FILTER (WHERE data->>'currency' IS NULL)::bigint     AS bad_currency
FROM records
WHERE table_name = 'holdings';

-- (2) schema_migrations summary: the backfill writes one row here via
-- INSERT ... ON CONFLICT DO NOTHING on UNIQUE(migration_name). Existence
-- of that row is a load-bearing signal that a *real* (non-dry-run)
-- backfill completed end-to-end on this database, not just that some
-- script touched migration_log. schema_migrations_rows is the exact count
-- (always 0 or 1 for this migration_name; > 1 would be a real bug).
SELECT
    COUNT(*)::bigint                                              AS schema_migrations_rows,
    MAX(applied_at)                                               AS schema_migrations_applied_at
FROM schema_migrations
WHERE migration_name = '{MIGRATION_NAME}';

-- (3) migration_log summary: one row per invocation, so total > 0 means
-- the backfill has been run at least once; success > 0 means at least
-- one of those runs actually committed (a backfill that only ever
-- dry-runs shouldn't pass verification). Same shape as
-- scripts/verify_migration.sql's migration_log block.
SELECT
    COUNT(*)::bigint                                              AS log_total,
    COUNT(*) FILTER (WHERE status = 'SUCCESS')::bigint            AS log_success,
    COUNT(*) FILTER (WHERE status = 'FAILED')::bigint             AS log_failed,
    COUNT(*) FILTER (WHERE status = 'DRY_RUN')::bigint            AS log_dry_run,
    (
        SELECT status
        FROM migration_log
        WHERE migration_name = '{MIGRATION_NAME}'
        ORDER BY run_at DESC
        LIMIT 1
    )                                                             AS last_status,
    (
        SELECT run_at
        FROM migration_log
        WHERE migration_name = '{MIGRATION_NAME}'
        ORDER BY run_at DESC
        LIMIT 1
    )                                                             AS last_run_at
FROM migration_log
WHERE migration_name = '{MIGRATION_NAME}';
"""

# Exit codes documented in the module docstring.
EXIT_PASS = 0
EXIT_FAIL = 1


def _collect_summaries(connection: psycopg.Connection) -> dict:
    """Run the verification SQL and return all three result sets as a dict.

    Returns:
        {
          "holdings":         {total_rows, bad_currency},
          "schema_migrations":{schema_migrations_rows, schema_migrations_applied_at},
          "migration_log":    {log_total, log_success, log_failed, log_dry_run,
                               last_status, last_run_at},
        }
    """
    with connection.cursor() as cursor:
        cursor.execute(_VERIFY_SQL)
        holdings_row = cursor.fetchone()
        holdings_cols = [d.name for d in cursor.description]

        cursor.nextset()
        schema_row = cursor.fetchone()
        schema_cols = [d.name for d in cursor.description]

        cursor.nextset()
        log_row = cursor.fetchone()
        log_cols = [d.name for d in cursor.description]

    if holdings_row is None or schema_row is None or log_row is None:
        raise RuntimeError(
            "verify_holdings_currency_backfill SQL returned fewer than "
            "three result sets; the SQL appears to be malformed"
        )

    return {
        "holdings": dict(zip(holdings_cols, holdings_row)),
        "schema_migrations": dict(zip(schema_cols, schema_row)),
        "migration_log": dict(zip(log_cols, log_row)),
    }


def _evaluate(
    holdings_summary: dict,
    schema_summary: dict,
    log_summary: dict,
) -> tuple[bool, list[str]]:
    """Return (passed, list_of_failure_reasons). Empty list means pass.

    The three checks mirror the three things the backfill is supposed to
    leave behind on a successful run:

      (a) Every holdings row has data->>'currency' IS NOT NULL.
      (b) schema_migrations has exactly one row for MIGRATION_NAME
          (idempotency guard: ON CONFLICT DO NOTHING means > 1 would be
          a real bug worth surfacing; 0 means the backfill never ran).
      (c) migration_log has at least one SUCCESS row for MIGRATION_NAME.
    """
    failures: list[str] = []

    if holdings_summary["bad_currency"] != 0:
        failures.append(
            f"{holdings_summary['bad_currency']} holdings row(s) still "
            f"have data->>'currency' IS NULL"
        )

    schema_rows = int(schema_summary["schema_migrations_rows"])
    if schema_rows == 0:
        failures.append(
            f"schema_migrations has no row for migration_name={MIGRATION_NAME!r}"
        )
    elif schema_rows > 1:
        # ON CONFLICT DO NOTHING on UNIQUE(migration_name) should make
        # this impossible. If it happens, surface it loudly -- it means
        # the UNIQUE constraint was removed or bypassed, which breaks
        # the idempotency guarantee.
        failures.append(
            f"schema_migrations has {schema_rows} rows for "
            f"migration_name={MIGRATION_NAME!r} (expected exactly 1)"
        )

    if log_summary["log_success"] == 0:
        failures.append(
            f"migration_log has no SUCCESS row for migration_name={MIGRATION_NAME!r}"
        )

    return (len(failures) == 0, failures)


def verify_holdings_currency_backfill(dsn: str = DEFAULT_POSTGRES_DSN) -> int:
    """Run the verification and return the exit code (0 == pass).

    Always prints summary counts to stdout. Writes failure reasons to
    stderr when something fails so ops/QA can see both at a glance."""
    with psycopg.connect(dsn, autocommit=True) as connection:
        summaries = _collect_summaries(connection)

    holdings = summaries["holdings"]
    schema = summaries["schema_migrations"]
    log = summaries["migration_log"]

    passed, failures = _evaluate(holdings, schema, log)
    log_success_flag = 1 if log["log_success"] > 0 else 0

    print(f"holdings.total_rows            = {holdings['total_rows']}")
    print(f"holdings.bad_currency          = {holdings['bad_currency']}")
    print(f"schema_migrations.rows         = {schema['schema_migrations_rows']}")
    print(f"migration_log.success          = {log_success_flag}")
    if not passed:
        print("", file=sys.stderr)
        for reason in failures:
            print(f"FAIL: {reason}", file=sys.stderr)

    return EXIT_PASS if passed else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(verify_holdings_currency_backfill())