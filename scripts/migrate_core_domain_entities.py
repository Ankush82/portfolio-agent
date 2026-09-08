"""Thin wrapper around scripts/migrate_core_domain_entities.sql.

Story STORY-11: creates the four core domain tables (users, portfolios,
holdings, transactions) with typed columns, primary keys, foreign keys,
unique constraints, and indexes.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN

_SQL_PATH = Path(__file__).with_name("migrate_core_domain_entities.sql")
_SQL = _SQL_PATH.read_text()

_TRUTHY = {"true", "1", "yes"}


def _is_dry_run() -> bool:
    raw = os.environ.get("MIGRATION_DRY_RUN", "")
    return raw.strip().lower() in _TRUTHY


# Real identity for this migration in migration_log -- verification scripts
# and idempotency/failure tests key off this exact name.
MIGRATION_NAME = "002_core_domain_entities_v1"

_LOG_INSERT_SQL = """
INSERT INTO migration_log (migration_name, run_at, status, rows_affected, error_message, dry_run)
VALUES (%s, now(), %s, %s, %s, %s)
"""


def _log(cursor, status: str, rows_affected: "int | None", error_message: "str | None", dry_run: bool) -> None:
    cursor.execute(_LOG_INSERT_SQL, (MIGRATION_NAME, status, rows_affected, error_message, dry_run))


def migrate_core_domain_entities(
    dsn: str = DEFAULT_POSTGRES_DSN,
    dry_run: bool | None = None,
) -> None:
    """Apply the core domain entities DDL (CREATE TABLE statements).

    Runs inside a single transaction via the SQL BEGIN/COMMIT in the .sql
    file.  Each invocation logs exactly one row to migration_log.
    """
    if dry_run is None:
        dry_run = _is_dry_run()

    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            try:
                cursor.execute(_SQL)
                _log(cursor, "SUCCESS", cursor.rowcount, None, dry_run)
            except Exception as exc:
                _log(cursor, "FAILED", None, str(exc), dry_run)
                raise


if __name__ == "__main__":
    migrate_core_domain_entities()
    mode = "DRY-RUN" if _is_dry_run() else "APPLIED"
    print(f"{mode}: core domain entities migration complete")
