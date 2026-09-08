"""Migration: broker_transactions table (STORY-15).

Adds a ``broker_transactions`` table keyed on
UNIQUE(user_id, broker_id, external_id) so import_transactions is
idempotent — re-running the import skips existing rows rather than
inserting duplicates.

Migration name: broker_transactions_v1
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN

MIGRATION_NAME = "broker_transactions_v1"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS broker_transactions (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    broker_id       TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    isin            TEXT NOT NULL,
    trade_date      DATE NOT NULL,
    side            TEXT NOT NULL,
    quantity        DECIMAL(18, 4) NOT NULL,
    price           DECIMAL(18, 4) NOT NULL,
    amount          DECIMAL(18, 4) NOT NULL,
    exchange        TEXT NOT NULL,
    segment         TEXT NOT NULL,
    raw             JSONB NOT NULL,
    imported_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, broker_id, external_id)
)
"""

_DROP_TABLE_SQL = """
DROP TABLE IF EXISTS broker_transactions
"""

_LOG_INSERT_SQL = """
INSERT INTO migration_log (migration_name, run_at, status, rows_affected, error_message, dry_run)
VALUES (%s, now(), %s, %s, %s, %s)
"""


def _is_dry_run() -> bool:
    raw = os.environ.get("MIGRATION_DRY_RUN", "")
    return raw.strip().lower() in {"true", "1", "yes"}


def migrate(
    dsn: str = DEFAULT_POSTGRES_DSN,
    dry_run: bool | None = None,
) -> None:
    """Apply the broker_transactions migration.

    Idempotent: CREATE TABLE IF NOT EXISTS means a second run is a
    no-op. Every invocation (dry-run, success, or failure) logs exactly
    one row to migration_log.
    """
    if dry_run is None:
        dry_run = _is_dry_run()

    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            try:
                if dry_run:
                    cursor.execute(
                        """
                        SELECT COUNT(*)
                        FROM information_schema.tables
                        WHERE table_name = 'broker_transactions'
                        """
                    )
                    exists = cursor.fetchone()[0] > 0
                    _log(cursor, "DRY_RUN", None if exists else 0, None, True)
                    return
                cursor.execute(_CREATE_TABLE_SQL)
                _log(cursor, "SUCCESS", cursor.rowcount, None, False)
            except Exception as exc:
                _log(cursor, "FAILED", None, str(exc), dry_run)
                raise


def rollback(
    dsn: str = DEFAULT_POSTGRES_DSN,
    dry_run: bool | None = None,
) -> None:
    """Roll back the broker_transactions migration (for verify_migration.py).

    Applies DROP TABLE IF EXISTS. Every invocation logs one row to
    migration_log.
    """
    if dry_run is None:
        dry_run = _is_dry_run()

    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            try:
                if dry_run:
                    cursor.execute(
                        """
                        SELECT COUNT(*)
                        FROM information_schema.tables
                        WHERE table_name = 'broker_transactions'
                        """
                    )
                    exists = cursor.fetchone()[0] > 0
                    _log(cursor, "DRY_RUN", None if not exists else 0, None, True)
                    return
                cursor.execute(_DROP_TABLE_SQL)
                _log(cursor, "SUCCESS", cursor.rowcount, None, False)
            except Exception as exc:
                _log(cursor, "FAILED", None, str(exc), dry_run)
                raise


def _log(cursor, status: str, rows_affected: "int | None", error_message: "str | None", dry_run: bool) -> None:
    cursor.execute(_LOG_INSERT_SQL, (MIGRATION_NAME, status, rows_affected, error_message, dry_run))


if __name__ == "__main__":
    migrate()
    print(f"APPLIED: {MIGRATION_NAME}")
