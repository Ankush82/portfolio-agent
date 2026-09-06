"""Thin wrapper around scripts/verify_migration.sql.

Story STORY-4 (issue #74): ops/QA script for confirming the US-stock
normalization migration applied correctly. Mirrors
scripts/migrate_us_stocks.py's structure (read SQL file, connect,
execute, summarize) but is strictly SELECT-only -- this script never
modifies the stocks table or migration_log.

STORY-13 adds additively-registered checks for the four core-domain tables
(users, portfolios, holdings, transactions) created by
migrate_core_domain_entities.sql.  The us_stock checks (STORY-4) are
unchanged and still pass independently of the core-domain checks.

Exit codes
----------
  0  All checks pass (us_stock + core-domain).
  1  At least one check failed. Summary counts are always printed so
     ops/QA can see exactly which one.

Summary counts printed regardless of pass/fail (us_stock path):
  * total rows in stocks
  * bad currency count (currency <> 'USD')
  * bad exchange count (exchange IS NOT NULL)
  * bad suffix count (symbol_suffix IS NOT NULL)
  * log success flag (1 if a SUCCESS row exists for the migration
    name, 0 otherwise)

Core-domain table checks (STORY-13):
  * per-table: exists flag, column checks, pk check, index checks
  * per-FK:   PRESENT / ABSENT (with orphan count when absent)
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN
from scripts.migrate_us_stocks import MIGRATION_NAME

_SQL_PATH = Path(__file__).with_name("verify_migration.sql")
_SQL = _SQL_PATH.read_text()

# STORY-13: additive check for the four core-domain tables.
# Kept in a separate SQL file (same directory) so the us_stock path
# is not touched, but the Python wrapper calls it additively.
_SQL_CD_PATH = Path(__file__).with_name("verify_migration_core_domain.sql")
_SQL_CD = _SQL_CD_PATH.read_text()

# Exit codes documented in the module docstring.
EXIT_PASS = 0
EXIT_FAIL = 1

# ---------------------------------------------------------------------------
# STORY-13: Core-domain table specs  (additively registered below)
# ---------------------------------------------------------------------------

# (name, expected_type_fragment, is_nullable)
#   type fragment is matched via SQL LIKE (%<fragment>%) on the
#   UPPER(data_type) || precision string returned by information_schema.
_CORE_DOMAIN_TABLE_SPECS: dict[str, dict] = {
    "users": {
        "expected_columns": [
            ("id",          "TEXT",    "NO"),
            ("email",       "TEXT",    "YES"),
            ("preferences", "JSONB",   "NO"),
            ("created_at",  "TIMESTAMPTZ", "NO"),
            ("updated_at",  "TIMESTAMPTZ", "NO"),
        ],
        "expected_indexes": ["idx_users_email"],
        "pk_column": "id",
    },
    "portfolios": {
        "expected_columns": [
            ("id",         "TEXT",    "NO"),
            ("user_id",    "TEXT",    "NO"),
            ("created_at", "TIMESTAMPTZ", "NO"),
            ("updated_at", "TIMESTAMPTZ", "NO"),
        ],
        "expected_indexes": ["idx_portfolios_user_id"],
        "pk_column": "id",
    },
    "holdings": {
        "expected_columns": [
            ("id",            "TEXT",  "NO"),
            ("portfolio_id",   "TEXT",  "NO"),
            ("security_id",    "TEXT",  "NO"),
            ("quantity",       "NUMERIC", "NO"),
            ("currency",      "TEXT",  "YES"),
            ("exchange",      "TEXT",  "YES"),
            ("symbol_suffix",  "TEXT",  "YES"),
            ("created_at",    "TIMESTAMPTZ", "NO"),
            ("updated_at",    "TIMESTAMPTZ", "NO"),
        ],
        "expected_indexes": ["idx_holdings_portfolio_id", "uq_holdings_portfolio_security"],
        "pk_column": "id",
    },
    "transactions": {
        "expected_columns": [
            ("id",           "TEXT",  "NO"),
            ("portfolio_id", "TEXT",  "NO"),
            ("kind",         "TEXT",  "NO"),
            ("amount",       "NUMERIC", "NO"),
            ("created_at",   "TIMESTAMPTZ", "NO"),
            ("updated_at",   "TIMESTAMPTZ", "NO"),
        ],
        "expected_indexes": ["idx_transactions_portfolio_id"],
        "pk_column": "id",
    },
}

# Result-set indices from verify_migration_core_domain.sql (0-based).
_RS_USERS_TABLE          = 0
_RS_USERS_COLS           = 1
_RS_USERS_PK             = 2
_RS_USERS_INDEXES        = 3
_RS_PORTFOLIOS_TABLE     = 4
_RS_PORTFOLIOS_COLS      = 5
_RS_PORTFOLIOS_PK        = 6
_RS_PORTFOLIOS_INDEXES   = 7
_RS_HOLDINGS_TABLE       = 8
_RS_HOLDINGS_COLS        = 9
_RS_HOLDINGS_PK          = 10
_RS_HOLDINGS_INDEXES     = 11
_RS_TRANSACTIONS_TABLE   = 12
_RS_TRANSACTIONS_COLS    = 13
_RS_TRANSACTIONS_PK      = 14
_RS_TRANSACTIONS_INDEXES = 15
_RS_FOREIGN_KEYS         = 16


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


# ---------------------------------------------------------------------------
# STORY-13 helpers (additively registered)
# ---------------------------------------------------------------------------

def _fetch_all_results(cursor: psycopg.Cursor) -> list[list[tuple]]:
    """Consume all result sets from a multi-statement SQL query.

    psycopg exposes each result set via cursor.nextset().
    """
    results: list[list[tuple]] = []
    while True:
        rows = cursor.fetchall()
        results.append(list(rows))
        if not cursor.nextset():
            break
    return results


def _check_core_domain_tables(connection: psycopg.Connection) -> list[str]:
    """Run the core-domain SQL and return a list of failure strings.

    Checks are additive: failures from this function are merged with
    failures from the us_stock path inside verify_migration().
    """
    failures: list[str] = []

    with connection.cursor() as cursor:
        cursor.execute(_SQL_CD)
        all_results = _fetch_all_results(cursor)

    table_order = ["users", "portfolios", "holdings", "transactions"]
    rs_indices = {
        "users":        (_RS_USERS_TABLE, _RS_USERS_COLS, _RS_USERS_PK, _RS_USERS_INDEXES),
        "portfolios":   (_RS_PORTFOLIOS_TABLE, _RS_PORTFOLIOS_COLS, _RS_PORTFOLIOS_PK, _RS_PORTFOLIOS_INDEXES),
        "holdings":     (_RS_HOLDINGS_TABLE, _RS_HOLDINGS_COLS, _RS_HOLDINGS_PK, _RS_HOLDINGS_INDEXES),
        "transactions":  (_RS_TRANSACTIONS_TABLE, _RS_TRANSACTIONS_COLS, _RS_TRANSACTIONS_PK, _RS_TRANSACTIONS_INDEXES),
    }

    for table in table_order:
        table_exists_flag, cols_idx, pk_idx, indexes_idx = rs_indices[table]
        spec = _CORE_DOMAIN_TABLE_SPECS[table]

        table_exists = len(all_results[table_exists_flag]) == 1
        print(f"{table}.exists = {1 if table_exists else 0}")

        if not table_exists:
            failures.append(f"table {table!r} does not exist")
            continue

        # Column checks
        actual_by_name = {r[0]: r for r in all_results[cols_idx]}
        for col_name, exp_type, exp_null in spec["expected_columns"]:
            if col_name not in actual_by_name:
                failures.append(f"{table}.column {col_name!r} is missing")
                continue
            _col, actual_type, actual_null = actual_by_name[col_name]
            if exp_type not in actual_type:
                failures.append(
                    f"{table}.column {col_name!r}: expected type containing {exp_type!r}, "
                    f"got {actual_type!r}"
                )
            if actual_null != exp_null:
                failures.append(
                    f"{table}.column {col_name!r}: expected nullability {exp_null!r}, "
                    f"got {actual_null!r}"
                )

        # Unexpected columns
        expected_names = {c[0] for c in spec["expected_columns"]}
        for col_name in set(actual_by_name) - expected_names:
            failures.append(f"{table}.unexpected column {col_name!r}")

        # PK check
        pk_ok = len(all_results[pk_idx]) == 1
        if pk_ok:
            print(f"{table}.pk = present")
        else:
            failures.append(f"{table}: primary key on {spec['pk_column']!r} is missing")

        # Index checks
        actual_idx_names = {r[2] for r in all_results[indexes_idx]}
        for idx_name in spec["expected_indexes"]:
            if idx_name not in actual_idx_names:
                failures.append(f"{table}.expected index {idx_name!r} is missing")
        if not any(idx_name not in actual_idx_names for idx_name in spec["expected_indexes"]):
            print(f"{table}.indexes = all present ({len(spec['expected_indexes'])})")

    # Foreign key checks
    fk_rows = all_results[_RS_FOREIGN_KEYS]
    fk_by_name = {r[0]: r for r in fk_rows}
    expected_fks = [
        ("fk_portfolios_user",       "portfolios",  "user_id",       "users"),
        ("fk_holdings_portfolio",    "holdings",    "portfolio_id",  "portfolios"),
        ("fk_transactions_portfolio","transactions","portfolio_id",  "portfolios"),
    ]
    for fk_name, _child, _col, _parent in expected_fks:
        if fk_name not in fk_by_name:
            failures.append(f"FK {fk_name!r}: cannot check (parent or child table missing)")
            continue
        _fk_name_val, fk_present_flag, orphan_count = fk_by_name[fk_name]
        fk_present = bool(fk_present_flag)
        orphans = int(orphan_count) if orphan_count is not None else 0
        if fk_present:
            print(f"FK {fk_name}: PRESENT")
        else:
            if orphans > 0:
                print(f"FK {fk_name}: ABSENT ({orphans} orphan row(s) — intentionally skipped by migration)")
            else:
                failures.append(
                    f"FK {fk_name!r}: ABSENT but 0 orphan rows — "
                    f"FK should have been added and was not"
                )

    return failures


def verify_migration(dsn: str = DEFAULT_POSTGRES_DSN) -> int:
    """Run the verification and return the exit code (0 == pass).

    Always prints summary counts to stdout. Writes failure reasons to
    stderr when something fails so ops/QA can see both at a glance.
    """
    with psycopg.connect(dsn, autocommit=True) as connection:
        summaries = _collect_summaries(connection)
        # STORY-13: additive core-domain table checks
        core_failures = _check_core_domain_tables(connection)
        failures.extend(core_failures)

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
