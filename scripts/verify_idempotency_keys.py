"""Thin wrapper around scripts/verify_idempotency_keys.sql.

STORY-188: ops/QA script for confirming the idempotency-key UNIQUE
constraints are in place and working. Mirrors
scripts/verify_migration.py's structure (read SQL file, connect,
execute, summarize) but is strictly SELECT-only -- this script never
modifies any table.

Exit codes
----------
  0  All checks pass:
       (a) holdings_idempotency_keys shadow table exists with a UNIQUE constraint;
       (b) transactions_idempotency_keys shadow table exists with a UNIQUE constraint;
       (c) _trg_holdings_idempotency_key trigger exists on records;
       (d) _trg_transactions_idempotency_key trigger exists on records;
       (e) migration_log has at least one row with
           migration_name='idempotency_keys_v1' and status='SUCCESS'.
  1  At least one check failed. Summary counts are always printed so
     ops/QA can see exactly which one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN
from scripts.migrate_idempotency_keys import MIGRATION_NAME

_SQL_PATH = Path(__file__).with_name("verify_idempotency_keys.sql")
_SQL = _SQL_PATH.read_text()

EXIT_PASS = 0
EXIT_FAIL = 1


def verify_idempotency_keys(dsn: str = DEFAULT_POSTGRES_DSN) -> int:
    """Run the verification and return the exit code (0 == pass).

    Always prints summary counts to stdout. Writes failure reasons to
    stderr when something fails.
    """
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(_SQL)
            rows = cursor.fetchall()
            cols = [d.name for d in cursor.description]

    results = [dict(zip(cols, row)) for row in rows]
    failures = [r for r in results if r["status"] != "OK"]

    print(f"checks.total   = {len(results)}")
    print(f"checks.passed = {len(results) - len(failures)}")
    print(f"checks.failed = {len(failures)}")
    for r in results:
        print(f"  {r['object_name'] or r['table_name']:50s} {r['status']}")

    if failures:
        print("", file=sys.stderr)
        for f in failures:
            name = f.get("object_name") or f.get("table_name")
            print(f"FAIL: {name} -> {f['status']}", file=sys.stderr)
        return EXIT_FAIL

    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(verify_idempotency_keys())
