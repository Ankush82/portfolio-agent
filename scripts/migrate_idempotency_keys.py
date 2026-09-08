"""Thin wrapper around scripts/migrate_idempotency_keys.sql.

STORY-188: applies (or dry-runs) the UNIQUE constraint additions on
broker_holding_id (holdings) and broker_transaction_id (transactions).

Gating is done in Python so the SQL file stays plain and reusable:
  * MIGRATION_DRY_RUN unset / falsy  -> run the real DDL, return its rowcount
  * MIGRATION_DRY_RUN truthy (case-insensitive true/1/yes) -> issue no DDL,
    return COUNT(*) of rows the shadow tables already hold (mirrors the
    end-state the triggers will reach after the real migration completes).

Every real invocation -- dry-run, successful real run, or a real failure --
logs exactly one row to migration_log.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN

_SQL_PATH = Path(__file__).with_name("migrate_idempotency_keys.sql")
_SQL = _SQL_PATH.read_text()

_TRUTHY = {"true", "1", "yes"}


def _is_dry_run() -> bool:
    raw = os.environ.get("MIGRATION_DRY_RUN", "")
    return raw.strip().lower() in _TRUTHY


# Dry-run query: count rows currently in both shadow tables combined.
# After a real migration completes, these counts represent the total
# number of holdings+transactions that have non-NULL broker_*_id values
# and are thus protected by the new UNIQUE constraints.
_COUNT_SQL = """
SELECT
    (SELECT COUNT(*) FROM holdings_idempotency_keys) +
    (SELECT COUNT(*) FROM transactions_idempotency_keys)
AS total_idempotency_keys
"""

# Real identity for this migration in migration_log -- the companion
# verify script and idempotency tests key off this exact name.
MIGRATION_NAME = "idempotency_keys_v1"

_LOG_INSERT_SQL = """
INSERT INTO migration_log (migration_name, run_at, status, rows_affected, error_message, dry_run)
VALUES (%s, now(), %s, %s, %s, %s)
"""


def _log(
    cursor,
    status: str,
    rows_affected: "int | None",
    error_message: "str | None",
    dry_run: bool,
) -> None:
    cursor.execute(
        _LOG_INSERT_SQL,
        (MIGRATION_NAME, status, rows_affected, error_message, dry_run),
    )


def migrate_idempotency_keys(
    dsn: str = DEFAULT_POSTGRES_DSN,
    dry_run: bool | None = None,
) -> int:
    """Apply UNIQUE constraints for broker_holding_id and broker_transaction_id.

    Creates shadow tables and triggers so that duplicate broker_holding_id or
    broker_transaction_id values raise a database-level integrity error,
    providing idempotency guarantees even when the application layer has a
    race condition.

    Returns the combined row count from both shadow tables. When dry_run is
    True, no DDL is issued and the count reflects current state.

    Every invocation (dry-run, real success, or real failure) logs exactly
    one row to migration_log.
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
                # Rowcount is not meaningful for DDL; report the shadow-table
                # count as the end-state metric.
                cursor.execute(_COUNT_SQL)
                rows = int(cursor.fetchone()[0])
                _log(cursor, "SUCCESS", rows, None, False)
                return rows
            except Exception as exc:
                _log(cursor, "FAILED", None, str(exc), dry_run)
                raise


if __name__ == "__main__":
    rows = migrate_idempotency_keys()
    mode = "DRY-RUN" if _is_dry_run() else "APPLIED"
    print(f"{mode}: shadow_table_keys={rows}")
