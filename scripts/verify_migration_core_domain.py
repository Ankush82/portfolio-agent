"""Verify the four core-domain tables created by migrate_core_domain_entities.

STORY-13: extends the repo's verification convention (docs/repo-layer-recon.md
item V6) to cover the tables added by STORY-12.

Mirrors scripts/verify_migration.py's CLI invocation style and output format
exactly as recorded in recon item V6. Exit codes match that convention:
  0  all checks pass
  1  at least one check failed

No env vars are introduced; the script uses DEFAULT_POSTGRES_DSN (the same
fallback default used by DefaultInfrastructure, per recon item V7).

Checks per table (users, portfolios, holdings, transactions):
  - table exists
  - every expected column exists with the expected data type and nullability
  - primary key on id exists
  - all expected indexes exist by name

Checks for foreign keys (fk_portfolios_user, fk_holdings_portfolio,
fk_transactions_portfolio):
  - FK present → always OK
  - FK absent but orphan_count > 0 → OK (STORY-12 deliberately skips adding
    an FK when legacy orphan rows are present; this state must be visible,
    not treated as a failure)
  - FK absent and orphan_count == 0 → FAIL (FK should have been added)
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN

_SQL_PATH = Path(__file__).with_name("verify_migration_core_domain.sql")
_SQL = _SQL_PATH.read_text()

# Exit codes matching the us_stock convention (recon item V6).
EXIT_PASS = 0
EXIT_FAIL = 1

# ---------------------------------------------------------------------------
# Table specs
# ---------------------------------------------------------------------------

# Columns: (name, expected_type_fragment, is_nullable)
#   expected_type_fragment is matched via SQL LIKE (%<fragment>%).
#   is_nullable: 'YES' or 'NO'.
_TABLE_SPECS: dict[str, dict] = {
    "users": {
        "expected_columns": [
            ("id",          "TEXT",                  "NO"),
            ("email",       "TEXT",                  "YES"),
            ("preferences", "JSONB",                 "NO"),
            ("created_at",  "TIMESTAMPTZ",            "NO"),
            ("updated_at",  "TIMESTAMPTZ",            "NO"),
        ],
        "expected_indexes": ["idx_users_email"],
        "pk_column": "id",
    },
    "portfolios": {
        "expected_columns": [
            ("id",         "TEXT",       "NO"),
            ("user_id",    "TEXT",       "NO"),
            ("created_at", "TIMESTAMPTZ", "NO"),
            ("updated_at", "TIMESTAMPTZ", "NO"),
        ],
        "expected_indexes": ["idx_portfolios_user_id"],
        "pk_column": "id",
    },
    "holdings": {
        "expected_columns": [
            ("id",            "TEXT",           "NO"),
            ("portfolio_id",  "TEXT",           "NO"),
            ("security_id",   "TEXT",           "NO"),
            ("quantity",      "NUMERIC",        "NO"),
            ("currency",      "TEXT",           "YES"),
            ("exchange",      "TEXT",           "YES"),
            ("symbol_suffix", "TEXT",           "YES"),
            ("created_at",    "TIMESTAMPTZ",     "NO"),
            ("updated_at",    "TIMESTAMPTZ",     "NO"),
        ],
        "expected_indexes": ["idx_holdings_portfolio_id", "uq_holdings_portfolio_security"],
        "pk_column": "id",
    },
    "transactions": {
        "expected_columns": [
            ("id",           "TEXT",           "NO"),
            ("portfolio_id", "TEXT",           "NO"),
            ("kind",         "TEXT",           "NO"),
            ("amount",       "NUMERIC",        "NO"),
            ("created_at",   "TIMESTAMPTZ",     "NO"),
            ("updated_at",   "TIMESTAMPTZ",     "NO"),
        ],
        "expected_indexes": ["idx_transactions_portfolio_id"],
        "pk_column": "id",
    },
}

# ---------------------------------------------------------------------------
# Expected foreign keys
# ---------------------------------------------------------------------------

EXPECTED_FK_DEFS = [
    {"name": "fk_portfolios_user",       "child": "portfolios",  "col": "user_id",       "parent": "users"},
    {"name": "fk_holdings_portfolio",    "child": "holdings",    "col": "portfolio_id",  "parent": "portfolios"},
    {"name": "fk_transactions_portfolio","child": "transactions","col": "portfolio_id",  "parent": "portfolios"},
]

# Result-set indices (0-based, after fetching all sets sequentially).
# Matched against psycopg's nextset() protocol.
_RS_USERS_TABLE        = 0
_RS_USERS_COLS         = 1
_RS_USERS_PK           = 2
_RS_USERS_INDEXES      = 3
_RS_PORTFOLIOS_TABLE   = 4
_RS_PORTFOLIOS_COLS    = 5
_RS_PORTFOLIOS_PK      = 6
_RS_PORTFOLIOS_INDEXES = 7
_RS_HOLDINGS_TABLE     = 8
_RS_HOLDINGS_COLS      = 9
_RS_HOLDINGS_PK        = 10
_RS_HOLDINGS_INDEXES   = 11
_RS_TRANSACTIONS_TABLE = 12
_RS_TRANSACTIONS_COLS  = 13
_RS_TRANSACTIONS_PK    = 14
_RS_TRANSACTIONS_INDEXES = 15
_RS_FOREIGN_KEYS       = 16


def _fetch_all_results(cursor: psycopg.Cursor) -> list[list[tuple]]:
    """Consume all result sets from a multi-statement SQL query.

    The SQL file contains 17 SELECT statements separated by semicolons.
    psycopg exposes them one at a time via cursor.nextset().
    """
    results: list[list[tuple]] = []
    while True:
        rows = cursor.fetchall()
        results.append(list(rows))
        if not cursor.nextset():
            break
    return results


def _check_table_exists(rows: list[tuple]) -> bool:
    """Return True if the exists_flag row was returned."""
    return len(rows) == 1 and rows[0][0] == 1


def _check_columns(rows: list[tuple], spec: list[tuple]) -> list[str]:
    """Check column names, types, and nullability. Returns failure reasons."""
    failures: list[str] = []
    # Index rows by column name for easy lookup
    actual: dict[str, tuple] = {r[0]: r for r in rows}
    expected_names = {c[0] for c in spec}

    # Missing columns
    for col_name, _exp_type, _exp_null in spec:
        if col_name not in actual:
            failures.append(f"column {col_name!r} is missing")

    # Unexpected columns
    actual_names = {r[0] for r in rows}
    for col_name in actual_names - expected_names:
        failures.append(f"unexpected column {col_name!r}")

    # Type and nullability
    for col_name, exp_type, exp_null in spec:
        if col_name not in actual:
            continue  # already reported as missing
        _col, actual_type, actual_null = actual[col_name]
        if exp_type not in actual_type:
            failures.append(
                f"column {col_name!r}: expected type containing {exp_type!r}, "
                f"got {actual_type!r}"
            )
        if actual_null != exp_null:
            failures.append(
                f"column {col_name!r}: expected nullability {exp_null!r}, "
                f"got {actual_null!r}"
            )

    return failures


def _check_pk(rows: list[tuple]) -> bool:
    """Return True if pk_present=1."""
    return len(rows) == 1 and rows[0][0] == 1


def _check_indexes(rows: list[tuple], expected: list[str]) -> list[str]:
    """Check that every expected index name is present. Returns failure reasons."""
    failures: list[str] = []
    actual_names = {r[2] for r in rows}  # pg_indexes rows: (schemaname, tablename, indexname, ...)
    for idx_name in expected:
        if idx_name not in actual_names:
            failures.append(f"expected index {idx_name!r} is missing")
    return failures


def verify_migration(dsn: str = DEFAULT_POSTGRES_DSN) -> int:
    """Run all checks and return exit code (0 == pass).

    Always prints a summary line to stdout per table.  Writes failure
    reasons to stderr when something fails, mirroring the us_stock
    convention (recon item V6).
    """
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(_SQL)
            all_results = _fetch_all_results(cursor)

    failures: list[str] = []

    # -------------------------------------------------------------------------
    # Per-table checks
    # -------------------------------------------------------------------------

    table_order = ["users", "portfolios", "holdings", "transactions"]
    rs_indices = {
        "users":        (_RS_USERS_TABLE, _RS_USERS_COLS, _RS_USERS_PK, _RS_USERS_INDEXES),
        "portfolios":   (_RS_PORTFOLIOS_TABLE, _RS_PORTFOLIOS_COLS, _RS_PORTFOLIOS_PK, _RS_PORTFOLIOS_INDEXES),
        "holdings":     (_RS_HOLDINGS_TABLE, _RS_HOLDINGS_COLS, _RS_HOLDINGS_PK, _RS_HOLDINGS_INDEXES),
        "transactions":  (_RS_TRANSACTIONS_TABLE, _RS_TRANSACTIONS_COLS, _RS_TRANSACTIONS_PK, _RS_TRANSACTIONS_INDEXES),
    }

    for table in table_order:
        table_exists_flag, cols_idx, pk_idx, indexes_idx = rs_indices[table]
        spec = _TABLE_SPECS[table]

        table_exists = _check_table_exists(all_results[table_exists_flag])
        print(f"{table}.exists = {1 if table_exists else 0}")

        if not table_exists:
            failures.append(f"table {table!r} does not exist")
            continue

        # Column checks
        col_failures = _check_columns(all_results[cols_idx], spec["expected_columns"])
        for f in col_failures:
            failures.append(f"{table}.{f}")

        # PK check
        if not _check_pk(all_results[pk_idx]):
            failures.append(f"{table}: primary key on {spec['pk_column']!r} is missing")
        else:
            print(f"{table}.pk = present")

        # Index checks
        idx_failures = _check_indexes(all_results[indexes_idx], spec["expected_indexes"])
        for f in idx_failures:
            failures.append(f"{table}.{f}")
        if not idx_failures:
            print(f"{table}.indexes = all present ({len(spec['expected_indexes'])})")

    # -------------------------------------------------------------------------
    # Foreign key checks
    # -------------------------------------------------------------------------

    fk_rows = all_results[_RS_FOREIGN_KEYS]
    fk_by_name = {r[0]: r for r in fk_rows}

    for fk_def in EXPECTED_FK_DEFS:
        name = fk_def["name"]
        if name not in fk_by_name:
            # Row not returned — parent/child tables must be absent
            failures.append(f"FK {name!r}: cannot check (one or both tables missing)")
            continue
        _fk_name, fk_present_flag, orphan_count = fk_by_name[name]
        fk_present = bool(fk_present_flag)
        orphan_count = int(orphan_count) if orphan_count is not None else 0

        if fk_present:
            print(f"FK {name}: PRESENT")
        else:
            if orphan_count > 0:
                print(f"FK {name}: ABSENT ({orphan_count} orphan row(s) — intentionally skipped by migration)")
            else:
                failures.append(
                    f"FK {name!r}: ABSENT but 0 orphan rows — "
                    f"FK should have been added and was not"
                )

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------

    if failures:
        print("", file=sys.stderr)
        for reason in failures:
            print(f"FAIL: {reason}", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(verify_migration())
