"""Tests for STORY-19: migration schema-drift and idempotency.

MIGRATED_TABLES (src/infrastructure_postgres.py) is a hand-written,
reviewable registry -- deliberately not runtime information_schema
introspection, so store/retrieve/query/delete depend on code a
reviewer can read rather than whatever the live DB schema happens to
be. That choice only stays safe as long as the registry and the real
migrated schema can't silently drift apart. This module is the guard:
if someone adds a column to migrate_core_domain_entities.sql but
forgets MIGRATED_TABLES (or vice versa), these tests fail loudly in CI
instead of the drift surfacing later as a confusing KeyError/DB error
in production.

Three checks, matching the story's acceptance criteria:
  1. Registry-vs-database drift: MIGRATED_TABLES vs information_schema,
     exact match on column names (explicitly accounting for the
     DB-managed created_at/updated_at columns the registry omits) and
     primary key, for all four core-domain tables.
  2. Migration idempotency: running scripts/run_migration.sh twice in a
     row succeeds both times, and the information_schema snapshot after
     the second run is identical to after the first.
  3. Verifier passes: scripts/verify_migration.py, invoked in-process
     the same way tests/test_verify_migration.py already does, exits 0
     against the migrated database.

Needs a live Postgres at DEFAULT_POSTGRES_DSN -- same precondition as
the rest of the integration tests in this repo (test_verify_migration.py,
test_story10_migrated_tables.py, test_repositories_postgres.py). Skips
cleanly via pytest.mark.skipif when unreachable; introduces no new env
var. Tests are re-runnable against the same database: every check
starts by (re-)applying the real migration, so state left over from a
prior run of this module -- or from a fresh, unmigrated database -- is
not a precondition.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import psycopg
import pytest

from infrastructure_postgres import DEFAULT_POSTGRES_DSN, MIGRATED_TABLES
from scripts.migrate_us_stocks import MIGRATION_NAME
from scripts.verify_migration import EXIT_PASS, verify_migration


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=1):
            return True
    except psycopg.Error:
        return False


POSTGRES_SKIP_REASON = (
    "no live Postgres reachable at DEFAULT_POSTGRES_DSN -- "
    "run `docker-compose up -d` for real coverage"
)
pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason=POSTGRES_SKIP_REASON
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_SCRIPT = _REPO_ROOT / "scripts" / "run_migration.sh"

# The migration's own DDL (migrate_core_domain_entities.sql) gives every
# core-domain table `created_at`/`updated_at TIMESTAMPTZ NOT NULL DEFAULT
# now()`, populated by Postgres itself. MIGRATED_TABLES deliberately
# omits both -- nothing in DefaultInfrastructure's store/retrieve/query/
# delete ever names them explicitly (STORY-10) -- so the drift check
# below must add them back in before comparing, or every table would
# report two "extra" columns that aren't really drift.
_DB_MANAGED_COLUMNS = {"created_at", "updated_at"}


def _run_migration() -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = DEFAULT_POSTGRES_DSN
    return subprocess.run(
        ["bash", str(_MIGRATION_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _snapshot_information_schema(conn: psycopg.Connection) -> dict[str, list[tuple]]:
    """Per-table (column_name, data_type, is_nullable, column_default)
    tuples for every MIGRATED_TABLES table, each list ordered by
    column_name so two snapshots compare equal regardless of the DDL
    application order that produced them."""
    snapshot: dict[str, list[tuple]] = {}
    with conn.cursor() as cur:
        for table in MIGRATED_TABLES:
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY column_name
                """,
                (table,),
            )
            snapshot[table] = cur.fetchall()
    return snapshot


@pytest.fixture(autouse=True)
def _ensure_migrated() -> None:
    """Every test in this module needs the real migration already
    applied -- run it once up front so each check starts from a
    known-good state regardless of what a prior test (in this module,
    or an unrelated one sharing the same local Postgres) left behind."""
    result = _run_migration()
    assert result.returncode == 0, (
        f"scripts/run_migration.sh failed in fixture setup: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_migrated_tables_registry_matches_information_schema_exactly():
    """STORY-19 check 1: MIGRATED_TABLES vs information_schema, exact
    match on column names (registry columns + the DB-managed
    created_at/updated_at it deliberately omits) and primary key, for
    all four core-domain tables."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            for table, spec in MIGRATED_TABLES.items():
                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    """,
                    (table,),
                )
                actual_columns = {row[0] for row in cur.fetchall()}
                expected_columns = set(spec.columns) | _DB_MANAGED_COLUMNS
                assert actual_columns == expected_columns, (
                    f"{table}: information_schema columns {sorted(actual_columns)} "
                    f"!= MIGRATED_TABLES columns {sorted(spec.columns)} + "
                    f"DB-managed {sorted(_DB_MANAGED_COLUMNS)} = "
                    f"{sorted(expected_columns)} -- registry and migration have "
                    f"drifted apart"
                )

                cur.execute(
                    """
                    SELECT kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON kcu.constraint_name = tc.constraint_name
                     AND kcu.table_schema = tc.table_schema
                    WHERE tc.table_schema = 'public'
                      AND tc.table_name = %s
                      AND tc.constraint_type = 'PRIMARY KEY'
                    """,
                    (table,),
                )
                pk_columns = [row[0] for row in cur.fetchall()]
                assert pk_columns == [spec.pk], (
                    f"{table}: primary key columns {pk_columns} != "
                    f"MIGRATED_TABLES pk {spec.pk!r}"
                )


def test_migration_applied_twice_both_runs_succeed():
    """STORY-19 check 2 (part 1): applying the migration twice in a row
    succeeds both times -- the second run is a real no-op convergence,
    not an error, on an already-migrated database."""
    first = _run_migration()
    assert first.returncode == 0, (
        f"first migration run failed: "
        f"stdout={first.stdout!r} stderr={first.stderr!r}"
    )

    second = _run_migration()
    assert second.returncode == 0, (
        f"second migration run failed: "
        f"stdout={second.stdout!r} stderr={second.stderr!r}"
    )


def test_migration_idempotent_information_schema_identical_after_second_run():
    """STORY-19 check 2 (part 2): the information_schema snapshot after
    a second migration run is identical to after the first -- applying
    the migration again changes nothing about the resulting schema."""
    first = _run_migration()
    assert first.returncode == 0, (
        f"first migration run failed: "
        f"stdout={first.stdout!r} stderr={first.stderr!r}"
    )
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        snapshot_after_first = _snapshot_information_schema(conn)

    second = _run_migration()
    assert second.returncode == 0, (
        f"second migration run failed: "
        f"stdout={second.stdout!r} stderr={second.stderr!r}"
    )
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        snapshot_after_second = _snapshot_information_schema(conn)

    assert snapshot_after_first == snapshot_after_second, (
        "information_schema snapshot changed between the first and second "
        "migration runs -- the migration is not idempotent:\n"
        f"after first:  {snapshot_after_first!r}\n"
        f"after second: {snapshot_after_second!r}"
    )


@pytest.fixture
def _stocks_and_log_seeded():
    """scripts/verify_migration.py's SQL also summarizes the (separate,
    STORY-4) `stocks` table and requires a migration_log SUCCESS row for
    the us-stock migration name -- neither is owned by the core-domain
    migration this module otherwise exercises. `stocks` is created here
    (private to this fixture, dropped in teardown -- nothing else has an
    FK into it). `migration_log` is real shared state other tests also
    write to (STORY-5's own tests document the same constraint), so only
    the one row this fixture inserts is removed, scoped by
    migration_name, never a blanket TRUNCATE."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS stocks (
                    id TEXT PRIMARY KEY,
                    currency TEXT,
                    exchange TEXT,
                    symbol_suffix TEXT
                )
                """
            )
            cur.execute(
                """
                INSERT INTO migration_log
                    (migration_name, run_at, status, rows_affected, error_message, dry_run)
                VALUES (%s, now(), 'SUCCESS', 0, NULL, false)
                """,
                (MIGRATION_NAME,),
            )

    yield

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS stocks")
            cur.execute(
                "DELETE FROM migration_log WHERE migration_name = %s", (MIGRATION_NAME,)
            )


def test_verify_migration_script_passes_against_migrated_database(
    _stocks_and_log_seeded,
):
    """STORY-19 check 3: scripts/verify_migration.py exits 0 against the
    migrated database. Invoked in-process via verify_migration() -- the
    same real entry point `if __name__ == "__main__"` calls -- matching
    the pattern tests/test_verify_migration.py already establishes for
    this script rather than introducing a second, subprocess-based way
    to invoke it."""
    exit_code = verify_migration()

    assert exit_code == EXIT_PASS, (
        f"scripts/verify_migration.py did not pass against a freshly "
        f"migrated database; exit_code={exit_code}"
    )
