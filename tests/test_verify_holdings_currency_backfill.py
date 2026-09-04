"""Tests for scripts/verify_holdings_currency_backfill.py (Story STORY-2).

Mirrors tests/test_verify_migration.py's structure on purpose (same
STORY-4 contract shape applied to STORY-2): a single live-Postgres
gate at module level, a scoped fixture that creates the real `records`
table this app stores everything in, and three passing/failing
scenarios that exercise the real exit-code contract.

Needs a live Postgres at DEFAULT_POSTGRES_DSN -- same precondition as
the rest of the integration tests in this repo.
"""

from __future__ import annotations

import uuid

import pytest
import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN
from scripts.backfill_holdings_currency import MIGRATION_NAME
from scripts.verify_holdings_currency_backfill import (
    EXIT_FAIL,
    EXIT_PASS,
    verify_holdings_currency_backfill,
)


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


# ---------------------------------------------------------------------------
# Shared fixtures & helpers
# ---------------------------------------------------------------------------


# One UUID for the entire pytest session -- changing per-call would
# make the fixture's teardown miss the rows seeded during the test, and
# every test in this module wants the same prefix so they don't trample
# each other (the `records` table is shared across all tests in this
# repo's suite).
_SESSION_UUID = uuid.uuid4().hex
_TEST_HOLDING_PREFIX = f"pf-vbhcb-{_SESSION_UUID}"
# SQL LIKE pattern: keep the trailing % so seeded ids starting with the
# prefix are caught by both setup (delete) and teardown (delete).
_TEST_HOLDING_PREFIX_LIKE = f"{_TEST_HOLDING_PREFIX}%"


@pytest.fixture
def records_table():
    """Create the canonical `records` table this app stores everything
    in (DefaultInfrastructure), then truncate the three tables the
    verification reads on teardown so we don't bleed state into the
    next test. Schema for `records` matches the fixture used in
    tests/test_backfill_holdings_currency.py so the verification is
    exercised against the same storage shape it would see in
    production."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    table_name TEXT NOT NULL,
                    id TEXT NOT NULL,
                    data JSONB NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (table_name, id)
                )
                """
            )
            # Clean state for everything verify_* touches. We use a
            # test-scoped portfolio prefix to scope holdings deletions so
            # other tests sharing this `records` table aren't disturbed.
            cur.execute(
                "DELETE FROM records WHERE table_name = 'holdings' "
                "AND id LIKE %s",
                (_TEST_HOLDING_PREFIX_LIKE,),
            )
            cur.execute(
                "DELETE FROM schema_migrations WHERE migration_name = %s",
                (MIGRATION_NAME,),
            )
            cur.execute(
                "DELETE FROM migration_log WHERE migration_name = %s",
                (MIGRATION_NAME,),
            )

    yield "records"

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM records WHERE table_name = 'holdings' "
                "AND id LIKE %s",
                (_TEST_HOLDING_PREFIX_LIKE,),
            )
            cur.execute(
                "DELETE FROM schema_migrations WHERE migration_name = %s",
                (MIGRATION_NAME,),
            )
            cur.execute(
                "DELETE FROM migration_log WHERE migration_name = %s",
                (MIGRATION_NAME,),
            )


def _seed_holdings(conn, holdings: list[tuple[str, dict]]) -> list[str]:
    """Seed `records` with `table_name='holdings'` and the given
    (id, data) pairs. Returns the seeded ids."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO records (table_name, id, data) VALUES (%s, %s, %s)",
            [("holdings", h_id, psycopg.types.json.Jsonb(data)) for h_id, data in holdings],
        )
    return [h_id for h_id, _ in holdings]


def _log_success(conn, rows_affected: int = 1) -> None:
    """Insert a SUCCESS row in migration_log for MIGRATION_NAME."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO migration_log
                (migration_name, run_at, status, rows_affected, error_message, dry_run)
            VALUES (%s, now(), 'SUCCESS', %s, NULL, false)
            """,
            (MIGRATION_NAME, rows_affected),
        )


def _log_failed(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO migration_log
                (migration_name, run_at, status, rows_affected, error_message, dry_run)
            VALUES (%s, now(), 'FAILED', NULL, %s, false)
            """,
            (MIGRATION_NAME, "boom"),
        )


def _log_dry_run(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO migration_log
                (migration_name, run_at, status, rows_affected, error_message, dry_run)
            VALUES (%s, now(), 'DRY_RUN', %s, NULL, true)
            """,
            (MIGRATION_NAME, 0),
        )


def _schema_migrations_row(conn) -> None:
    """Insert the canonical schema_migrations row for MIGRATION_NAME
    (the one the backfill writes via INSERT ... ON CONFLICT DO NOTHING)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schema_migrations (migration_name, applied_at) "
            "VALUES (%s, now())",
            (MIGRATION_NAME,),
        )


# ---------------------------------------------------------------------------
# Passing scenario: every holdings row has currency='USD',
# schema_migrations has the row, migration_log has SUCCESS.
# ---------------------------------------------------------------------------


def test_verify_passes_when_all_holdings_have_currency_and_logs_record_success(
    records_table, capsys
):
    """Seed: 3 holdings rows (all currency='USD' in their data JSONB),
    one schema_migrations row for MIGRATION_NAME, one migration_log
    SUCCESS row for MIGRATION_NAME. Expect exit code 0."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_holdings(
            conn,
            [
                (f"{_TEST_HOLDING_PREFIX}-AAPL", {"portfolio_id": "pf-vbh", "security_id": "AAPL", "currency": "USD"}),
                (f"{_TEST_HOLDING_PREFIX}-MSFT", {"portfolio_id": "pf-vbh", "security_id": "MSFT", "currency": "USD"}),
                (f"{_TEST_HOLDING_PREFIX}-GOOGL", {"portfolio_id": "pf-vbh", "security_id": "GOOGL", "currency": "USD"}),
            ],
        )
        _schema_migrations_row(conn)
        _log_success(conn, rows_affected=3)

    exit_code = verify_holdings_currency_backfill()
    captured = capsys.readouterr()

    assert exit_code == EXIT_PASS, (
        f"verify_holdings_currency_backfill must exit 0 when every "
        f"holdings row has currency='USD', schema_migrations has the row, "
        f"and migration_log has a SUCCESS row; got exit_code={exit_code}"
    )
    # Summary counts are always printed -- pass or fail.
    assert "holdings.total_rows" in captured.out
    assert "holdings.bad_currency" in captured.out
    assert "schema_migrations.rows" in captured.out
    assert "migration_log.success" in captured.out


# ---------------------------------------------------------------------------
# Failing scenario #1: a holdings row is missing currency in its data JSONB.
# ---------------------------------------------------------------------------


def test_verify_fails_when_any_holdings_row_is_missing_currency(
    records_table, capsys
):
    """Seed: 1 good holding + 1 holding whose data JSONB has no
    `currency` key. Expect exit code 1 even though schema_migrations
    and migration_log are populated."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_holdings(
            conn,
            [
                (f"{_TEST_HOLDING_PREFIX}-GOOD", {"portfolio_id": "pf-vbh", "security_id": "GOOD", "currency": "USD"}),
                # Missing currency key: backfill never ran on this row.
                (f"{_TEST_HOLDING_PREFIX}-BAD", {"portfolio_id": "pf-vbh", "security_id": "BAD", "quantity": 5}),
            ],
        )
        _schema_migrations_row(conn)
        _log_success(conn)

    exit_code = verify_holdings_currency_backfill()
    captured = capsys.readouterr()

    assert exit_code == EXIT_FAIL, (
        f"verify_holdings_currency_backfill must exit non-zero when a "
        f"holdings row has data->>'currency' IS NULL; got exit_code={exit_code}"
    )
    # Summary still printed on failure.
    assert "holdings.bad_currency" in captured.out
    # And the failure reason is written to stderr for ops/QA.
    assert "FAIL:" in captured.err
    assert "currency" in captured.err.lower(), (
        f"failure reason must mention the missing currency; got "
        f"stderr={captured.err!r}"
    )


# ---------------------------------------------------------------------------
# Failing scenario #2: holdings are fine but schema_migrations has no row.
# ---------------------------------------------------------------------------


def test_verify_fails_when_schema_migrations_has_no_row(
    records_table, capsys
):
    """Seed: holdings are fully normalized, migration_log has SUCCESS,
    but schema_migrations is empty for MIGRATION_NAME. Expect exit
    code 1. Catches the case where someone manually ran the migration
    SQL without going through backfill_holdings_currency.py."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_holdings(
            conn,
            [(f"{_TEST_HOLDING_PREFIX}-AAPL", {"portfolio_id": "pf-vbh", "security_id": "AAPL", "currency": "USD"})],
        )
        # NO _schema_migrations_row() call -- the backfill's logical-
        # migration record is the thing that's missing.
        _log_success(conn)

    exit_code = verify_holdings_currency_backfill()
    captured = capsys.readouterr()

    assert exit_code == EXIT_FAIL, (
        f"missing schema_migrations row must fail verification; "
        f"got exit_code={exit_code}"
    )
    assert "schema_migrations" in captured.err.lower(), (
        f"failure reason must mention schema_migrations; got "
        f"stderr={captured.err!r}"
    )


# ---------------------------------------------------------------------------
# Failing scenario #3: holdings and schema_migrations are fine but
# migration_log only has DRY_RUN rows (no SUCCESS).
# ---------------------------------------------------------------------------


def test_verify_fails_when_migration_log_only_has_dry_run(
    records_table, capsys
):
    """Seed: holdings fully normalized, schema_migrations row present,
    migration_log has only a DRY_RUN row. Expect exit code 1 -- this
    is the story's "verification can be run after a dry-run (dry-run
    should fail verification)" equivalent for STORY-2."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_holdings(
            conn,
            [(f"{_TEST_HOLDING_PREFIX}-AAPL", {"portfolio_id": "pf-vbh", "security_id": "AAPL", "currency": "USD"})],
        )
        _schema_migrations_row(conn)
        _log_dry_run(conn)  # only DRY_RUN, no SUCCESS

    exit_code = verify_holdings_currency_backfill()
    captured = capsys.readouterr()

    assert exit_code == EXIT_FAIL, (
        f"a DRY_RUN-only migration_log must fail verification; "
        f"got exit_code={exit_code}"
    )
    assert "migration_log.success" in captured.out
    assert "FAIL:" in captured.err


# ---------------------------------------------------------------------------
# Failing scenario #4: schema_migrations has the row but migration_log
# has only a FAILED row (the backfill attempted and rolled back).
# ---------------------------------------------------------------------------


def test_verify_fails_when_migration_log_only_has_failed(
    records_table, capsys
):
    """Seed: holdings normalized (assume a prior successful run is what
    left them this way, or that the data was inserted post-migration),
    schema_migrations row present, migration_log only FAILED. Expect
    exit code 1."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_holdings(
            conn,
            [(f"{_TEST_HOLDING_PREFIX}-AAPL", {"portfolio_id": "pf-vbh", "security_id": "AAPL", "currency": "USD"})],
        )
        _schema_migrations_row(conn)
        _log_failed(conn)

    exit_code = verify_holdings_currency_backfill()

    assert exit_code == EXIT_FAIL, (
        f"a FAILED-only migration_log must fail verification; "
        f"got exit_code={exit_code}"
    )


# ---------------------------------------------------------------------------
# Failing scenario #5: schema_migrations has > 1 row for MIGRATION_NAME
# (the ON CONFLICT DO NOTHING invariant is broken).
# ---------------------------------------------------------------------------


def test_verify_fails_when_schema_migrations_has_duplicate_rows(
    records_table, capsys
):
    """Seed: schema_migrations has two rows for MIGRATION_NAME (which
    should be impossible under the UNIQUE constraint, but if someone
    drops it the verification should surface the anomaly). The
    schema_migrations UNIQUE constraint exists in production schema,
    so we DROP it for this test only and re-CREATE on teardown."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE schema_migrations DROP CONSTRAINT IF EXISTS schema_migrations_migration_name_key"
            )

    try:
        with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
            _seed_holdings(
                conn,
                [(f"{_TEST_HOLDING_PREFIX}-AAPL", {"portfolio_id": "pf-vbh", "security_id": "AAPL", "currency": "USD"})],
            )
            # Insert two rows -- only possible because we just dropped
            # the UNIQUE constraint.
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO schema_migrations (migration_name, applied_at) "
                    "VALUES (%s, now())",
                    [(MIGRATION_NAME,), (MIGRATION_NAME,)],
                )
            _log_success(conn)

        exit_code = verify_holdings_currency_backfill()
        captured = capsys.readouterr()

        assert exit_code == EXIT_FAIL, (
            f">1 schema_migrations row for {MIGRATION_NAME} must fail "
            f"verification; got exit_code={exit_code}"
        )
        assert "expected exactly 1" in captured.err, (
            f"failure reason must mention the >1 row count; got "
            f"stderr={captured.err!r}"
        )
    finally:
        # Restore the UNIQUE constraint for the next test.
        with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM schema_migrations WHERE migration_name = %s",
                    (MIGRATION_NAME,),
                )
                cur.execute(
                    "ALTER TABLE schema_migrations "
                    "ADD CONSTRAINT schema_migrations_migration_name_key "
                    "UNIQUE (migration_name)"
                )


# ---------------------------------------------------------------------------
# End-to-end: run the real backfill, then verify, then verify again.
# ---------------------------------------------------------------------------


def test_verify_after_real_backfill_passes(records_table, monkeypatch, capsys):
    """End-to-end smoke test: seed un-normalized holdings, run the real
    backfill (not dry-run), then run verify_holdings_currency_backfill.
    Expect exit code 0. Proves the backfill and the verifier agree on
    what 'done' looks like."""
    from scripts.backfill_holdings_currency import backfill_holdings_currency

    monkeypatch.setenv("MIGRATION_DRY_RUN", "false")

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_holdings(
            conn,
            [
                # Three rows missing currency -- the backfill should add
                # currency='USD' to all three.
                (f"{_TEST_HOLDING_PREFIX}-E2E-MSFT", {"portfolio_id": "pf-vbh", "security_id": "MSFT", "quantity": 5}),
                (f"{_TEST_HOLDING_PREFIX}-E2E-GOOG", {"portfolio_id": "pf-vbh", "security_id": "GOOG", "quantity": 3}),
                (f"{_TEST_HOLDING_PREFIX}-E2E-TSLA", {"portfolio_id": "pf-vbh", "security_id": "TSLA", "quantity": 7}),
            ],
        )

    rows_updated = backfill_holdings_currency()
    assert rows_updated == 3, f"backfill should have updated 3 rows; got {rows_updated}"

    exit_code = verify_holdings_currency_backfill()

    assert exit_code == EXIT_PASS, (
        f"verification must pass after a successful real backfill; "
        f"got exit_code={exit_code}"
    )


# ---------------------------------------------------------------------------
# Read-only contract: the verification must not modify data.
# ---------------------------------------------------------------------------


def test_verify_does_not_modify_data(records_table, capsys):
    """Capture every row in `records` (scoped to this test's prefix) and
    migration_log/schema_migrations (scoped to MIGRATION_NAME) before
    running verify_holdings_currency_backfill against a deliberately
    failing scenario, and assert nothing changed afterwards. This is
    the load-bearing guarantee behind 'this script is SELECT-only'."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_holdings(
            conn,
            [
                # One normalized + one missing -> fails verification but
                # mustn't write anything.
                (f"{_TEST_HOLDING_PREFIX}-GOOD", {"portfolio_id": "pf-vbh", "security_id": "GOOD", "currency": "USD"}),
                (f"{_TEST_HOLDING_PREFIX}-BAD", {"portfolio_id": "pf-vbh", "security_id": "BAD"}),
            ],
        )
        _schema_migrations_row(conn)
        _log_failed(conn)

        # Snapshot.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, data FROM records WHERE table_name = 'holdings' "
                "AND id LIKE %s ORDER BY id",
                (_TEST_HOLDING_PREFIX_LIKE,),
            )
            records_before = cur.fetchall()
            cur.execute(
                "SELECT migration_name, status, rows_affected, dry_run, error_message "
                "FROM migration_log WHERE migration_name = %s ORDER BY id",
                (MIGRATION_NAME,),
            )
            log_before = cur.fetchall()
            cur.execute(
                "SELECT migration_name, applied_at FROM schema_migrations "
                "WHERE migration_name = %s ORDER BY id",
                (MIGRATION_NAME,),
            )
            schema_before = cur.fetchall()

    # Run verify -- this should fail (one holdings row still has no
    # currency) but never write anything.
    exit_code = verify_holdings_currency_backfill()
    assert exit_code == EXIT_FAIL, (
        "missing-currency holdings row must trigger non-zero exit"
    )

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, data FROM records WHERE table_name = 'holdings' "
                "AND id LIKE %s ORDER BY id",
                (_TEST_HOLDING_PREFIX_LIKE,),
            )
            records_after = cur.fetchall()
            cur.execute(
                "SELECT migration_name, status, rows_affected, dry_run, error_message "
                "FROM migration_log WHERE migration_name = %s ORDER BY id",
                (MIGRATION_NAME,),
            )
            log_after = cur.fetchall()
            cur.execute(
                "SELECT migration_name, applied_at FROM schema_migrations "
                "WHERE migration_name = %s ORDER BY id",
                (MIGRATION_NAME,),
            )
            schema_after = cur.fetchall()

    assert records_after == records_before, (
        "verify_holdings_currency_backfill must not modify any row in records"
    )
    assert log_after == log_before, (
        "verify_holdings_currency_backfill must not modify any row in migration_log"
    )
    assert schema_after == schema_before, (
        "verify_holdings_currency_backfill must not modify any row in schema_migrations"
    )