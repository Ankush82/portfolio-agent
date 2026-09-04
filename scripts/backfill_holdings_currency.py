"""Backfill holdings.currency='USD' for existing holdings rows
that don't already carry a currency, then record the application in
schema_migrations so a second run is a no-op.

Story STORY-1: real storage is `records` (DefaultInfrastructure --
generic JSONB), so the backfill targets
`records.data->>'currency' IS NULL` for `records.table_name='holdings'`
rather than any per-column ALTER TABLE.

Mirrors scripts/migrate_us_stocks.py's structure on purpose:
  * MIGRATION_DRY_RUN env-var gating (case-insensitive true/1/yes)
  * Real autocommit connection (one implicit transaction per
    statement, so a failed UPDATE rolls back on its own)
  * One migration_log row per invocation (success/dry-run/failure)
  * Distinct MIGRATION_NAME so other tests can scope to this
    migration's log rows

Differences from migrate_us_stocks.py:
  * Operates on the `records` JSONB table (this app's real storage),
    not a dedicated `stocks` table.
  * Uses jsonb_set() to add the new key without clobbering any other
    fields in `data` — a wholesale `data = ...` rewrite would erase
    every other field on existing holdings rows.
  * Also writes a row to `schema_migrations` (the new table from this
    story's `_ensure_schema` change), guarded by an INSERT ... ON
    CONFLICT DO NOTHING so the backfill is idempotent at the schema-
    migrations level even if the records UPDATE itself is a no-op.
"""

from __future__ import annotations

import os

import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN

_TRUTHY = {"true", "1", "yes"}

# Real identity for this backfill in migration_log. The schema_migrations
# row is keyed off the same name; both consumers (issue #74-style
# verification scripts, idempotency/failure tests) read MIGRATION_NAME
# from this module so it isn't just documentation.
MIGRATION_NAME = "holdings_currency_backfill_v1"

# Defaults this backfill applies. currency is the only field STORY-1's
# backfill needs to set; exchange/symbol_suffix stay NULL on pre-existing
# rows because the dataclass default for those is None.
_BACKFILL_CURRENCY = "USD"

# UPDATE: set data->>'currency' to 'USD' on every holdings row that
# doesn't already have one. jsonb_set creates the key if absent and
# leaves every other key in `data` untouched (a wholesale
# `data = '{"currency": "USD"}'::jsonb` rewrite would clobber the
# portfolio_id, security_id, quantity, etc.).
_UPDATE_SQL = """
UPDATE records
SET data = jsonb_set(data, '{currency}', to_jsonb(%s::text))
WHERE table_name = 'holdings'
  AND data->>'currency' IS NULL
"""

# COUNT for the dry-run path: exact same WHERE clause as _UPDATE_SQL so
# the two numbers agree on every input (the dry-run must predict the
# real run's rowcount).
_COUNT_SQL = """
SELECT COUNT(*)
FROM records
WHERE table_name = 'holdings'
  AND data->>'currency' IS NULL
"""

# Log row in migration_log (existing, per-invocation table — this is
# what migrate_us_stocks.py writes too, so the same monitoring queries
# work for both migrations).
_LOG_INSERT_SQL = """
INSERT INTO migration_log (migration_name, run_at, status, rows_affected, error_message, dry_run)
VALUES (%s, now(), %s, %s, %s, %s)
"""

# Logical-migration row in schema_migrations (new this story). ON
# CONFLICT DO NOTHING makes the backfill idempotent at this layer even
# when the records UPDATE is itself a no-op (every row already has
# currency='USD' from a prior run, or from being created after the
# dataclass started carrying the field).
_SCHEMA_MIGRATION_INSERT_SQL = """
INSERT INTO schema_migrations (migration_name, applied_at)
VALUES (%s, now())
ON CONFLICT (migration_name) DO NOTHING
"""


def _is_dry_run() -> bool:
    raw = os.environ.get("MIGRATION_DRY_RUN", "")
    return raw.strip().lower() in _TRUTHY


def _ensure_schema_migrations_table(connection: psycopg.Connection) -> None:
    """Create schema_migrations if it isn't already there. The story's
    `_ensure_schema` change adds it for DefaultInfrastructure, but the
    backfill script's own connection may not have gone through that
    code path yet (e.g. it's the first thing run against a fresh
    database). CREATE TABLE IF NOT EXISTS is idempotent and matches
    the rest of this project's style, so running it here is cheap and
    self-contained."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id BIGSERIAL PRIMARY KEY,
                migration_name VARCHAR NOT NULL UNIQUE,
                applied_at TIMESTAMPTZ NOT NULL
            )
            """
        )


def _log(cursor, status: str, rows_affected: "int | None", error_message: "str | None", dry_run: bool) -> None:
    cursor.execute(_LOG_INSERT_SQL, (MIGRATION_NAME, status, rows_affected, error_message, dry_run))


def backfill_holdings_currency(
    dsn: str = DEFAULT_POSTGRES_DSN,
    dry_run: bool | None = None,
) -> int:
    """Apply the holdings.currency='USD' backfill.

    Returns rows_updated. When dry_run is True, no UPDATE is issued and
    the count reflects rows that would have changed. Every invocation —
    dry-run, successful real run, or failure — logs exactly one row to
    migration_log; successful real runs additionally write one row to
    schema_migrations (idempotent: a second successful real run is a
    no-op at this layer). Each connection here is autocommit (one
    implicit transaction per statement), so a failed real UPDATE
    rolls back on its own before the exception is caught and logged —
    no explicit BEGIN/ROLLBACK needed for this single-statement
    migration."""
    if dry_run is None:
        dry_run = _is_dry_run()

    with psycopg.connect(dsn, autocommit=True) as connection:
        _ensure_schema_migrations_table(connection)
        with connection.cursor() as cursor:
            try:
                if dry_run:
                    cursor.execute(_COUNT_SQL)
                    rows = int(cursor.fetchone()[0])
                    _log(cursor, "DRY_RUN", rows, None, True)
                    return rows
                cursor.execute(_UPDATE_SQL, (_BACKFILL_CURRENCY,))
                rows = cursor.rowcount
                # Mark the logical migration as applied — ON CONFLICT
                # DO NOTHING means a re-run still gets logged to
                # migration_log (per-invocation) but only ever produces
                # one schema_migrations row.
                cursor.execute(_SCHEMA_MIGRATION_INSERT_SQL, (MIGRATION_NAME,))
                _log(cursor, "SUCCESS", rows, None, False)
                return rows
            except Exception as exc:
                _log(cursor, "FAILED", None, str(exc), dry_run)
                raise


if __name__ == "__main__":
    rows = backfill_holdings_currency()
    mode = "DRY-RUN" if _is_dry_run() else "APPLIED"
    print(f"{mode}: rows_updated={rows}")
