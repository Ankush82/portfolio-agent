"""Tests for scripts/verify_migration.{sql,py} (Story STORY-4).

Needs a live Postgres at DEFAULT_POSTGRES_DSN -- same precondition as
the rest of the integration tests in this repo.
"""

from __future__ import annotations

import pytest
import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN
from scripts.migrate_us_stocks import MIGRATION_NAME
from scripts.verify_migration import (
    EXIT_FAIL,
    EXIT_PASS,
    verify_migration,
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


@pytest.fixture
def stocks_and_log_tables():
    """Create the canonical `stocks` table for this story. The
    migration_log table is created idempotently by
    infrastructure_postgres, but for hermetic test isolation we
    truncate migration_log on setup and again on teardown so rows
    from prior runs don't bleed into a passing scenario."""
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
            # migration_log exists already (DefaultInfrastructure._ensure_schema),
            # but truncate it so prior runs from this or other tests
            # can't accidentally make a failing scenario look passing.
            cur.execute("TRUNCATE TABLE migration_log")
            # STORY-13: ensure the four core-domain tables exist with correct
            # DDL so the additive core-domain checks in verify_migration.py
            # find them in place.  Each table is created with its target
            # schema including PK, indexes, and FKs where applicable.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id           TEXT        NOT NULL,
                    email        TEXT,
                    preferences  JSONB      NOT NULL DEFAULT '{}'::jsonb,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (id)
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_email ON users (email)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolios (
                    id         TEXT        NOT NULL,
                    user_id    TEXT        NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (id)
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_portfolios_user_id ON portfolios (user_id)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS holdings (
                    id             TEXT            NOT NULL,
                    portfolio_id   TEXT            NOT NULL,
                    security_id    TEXT            NOT NULL,
                    quantity       NUMERIC(38, 10) NOT NULL DEFAULT 0,
                    currency       TEXT,
                    exchange       TEXT,
                    symbol_suffix  TEXT,
                    created_at     TIMESTAMPTZ     NOT NULL DEFAULT now(),
                    updated_at     TIMESTAMPTZ     NOT NULL DEFAULT now(),
                    PRIMARY KEY (id)
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_holdings_portfolio_id ON holdings (portfolio_id)"
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_holdings_portfolio_security ON holdings (portfolio_id, security_id)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id           TEXT            NOT NULL,
                    portfolio_id TEXT            NOT NULL,
                    kind         TEXT            NOT NULL,
                    amount       NUMERIC(38, 10) NOT NULL,
                    created_at   TIMESTAMPTZ     NOT NULL DEFAULT now(),
                    updated_at   TIMESTAMPTZ     NOT NULL DEFAULT now(),
                    PRIMARY KEY (id)
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_portfolio_id ON transactions (portfolio_id)"
            )

    yield

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS stocks")
            cur.execute("TRUNCATE TABLE migration_log")
            # STORY-13: clean up core-domain tables created by this fixture.
            cur.execute("DROP TABLE IF EXISTS transactions")
            cur.execute("DROP TABLE IF EXISTS holdings")
            cur.execute("DROP TABLE IF EXISTS portfolios")
            cur.execute("DROP TABLE IF EXISTS users")


def _seed_normalized(conn, ids: list[str]) -> None:
    """Insert `ids` into stocks all already normalized
    (currency='USD', exchange=NULL, symbol_suffix=NULL)."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO stocks (id, currency, exchange, symbol_suffix) "
            "VALUES (%s, %s, %s, %s)",
            [(id_, "USD", None, None) for id_ in ids],
        )


def _seed_one_bad_row(conn) -> None:
    """Insert one good row + one row whose currency is wrong so
    bad_currency ends up >= 1."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO stocks (id, currency, exchange, symbol_suffix) "
            "VALUES (%s, %s, %s, %s)",
            [
                ("GOOD", "USD", None, None),
                ("BAD", "EUR", None, None),
            ],
        )


def _log_success(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO migration_log
                (migration_name, run_at, status, rows_affected, error_message, dry_run)
            VALUES (%s, now(), 'SUCCESS', %s, NULL, false)
            """,
            (MIGRATION_NAME, 1),
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


# ---------------------------------------------------------------------------
# Acceptance criterion #1 + #2 + #3: exit code 0 when everything matches,
# non-zero otherwise. Summary counts are always printed.
# ---------------------------------------------------------------------------


def test_verify_migration_passes_when_stocks_normalized_and_log_has_success(
    stocks_and_log_tables, capsys
):
    """Passing scenario: stocks are all currency='USD', exchange/suffix
    NULL; migration_log has at least one SUCCESS row for the migration
    name. Expect exit code 0."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_normalized(conn, ["AAPL", "MSFT", "GOOGL"])
        _log_success(conn)

    exit_code = verify_migration()
    captured = capsys.readouterr()

    assert exit_code == EXIT_PASS, (
        f"verify_migration must exit 0 when stocks are normalized and "
        f"migration_log has a SUCCESS row; got exit_code={exit_code}"
    )
    # Summary counts must always be printed, pass or fail.
    assert "stocks.total_rows" in captured.out
    assert "stocks.bad_currency" in captured.out
    assert "stocks.bad_exchange" in captured.out
    assert "stocks.bad_suffix" in captured.out
    assert "migration_log.success" in captured.out


def test_verify_migration_fails_when_any_stock_row_is_bad(
    stocks_and_log_tables, capsys
):
    """Failing scenario #1: at least one stock row has currency<>'USD'.
    Expect exit code 1 even though migration_log has a SUCCESS row."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_one_bad_row(conn)
        _log_success(conn)

    exit_code = verify_migration()
    captured = capsys.readouterr()

    assert exit_code == EXIT_FAIL, (
        f"verify_migration must exit non-zero when a stock row has "
        f"currency<>'USD'; got exit_code={exit_code}"
    )
    # Summary still printed on failure.
    assert "stocks.bad_currency" in captured.out
    # And the failure reason is written to stderr for ops/QA.
    assert "FAIL:" in captured.err


def test_verify_migration_fails_when_migration_log_has_no_success_row(
    stocks_and_log_tables, capsys
):
    """Failing scenario #2: stocks are normalized but migration_log has
    no SUCCESS row (only a DRY_RUN row, e.g.). Expect exit code 1.
    Covers the story's 'verification can be run after a dry-run (dry-run
    should fail verification unless adjusted)' clause."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_normalized(conn, ["AAPL"])
        _log_dry_run(conn)  # only DRY_RUN, no SUCCESS row

    exit_code = verify_migration()
    captured = capsys.readouterr()

    assert exit_code == EXIT_FAIL, (
        f"a DRY_RUN-only migration_log must fail verification; "
        f"got exit_code={exit_code}"
    )
    assert "migration_log.success" in captured.out
    assert "FAIL:" in captured.err


def test_verify_migration_fails_when_migration_log_empty(
    stocks_and_log_tables, capsys
):
    """Failing scenario #3: stocks are normalized but migration_log has
    no rows at all for this migration. Expect exit code 1."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_normalized(conn, ["AAPL"])
        # migration_log truncated by the fixture -- nothing inserted.

    exit_code = verify_migration()

    assert exit_code == EXIT_FAIL, (
        f"missing migration_log row must fail verification; "
        f"got exit_code={exit_code}"
    )


def test_verify_migration_summary_counts_match_real_db_state_on_pass(
    stocks_and_log_tables, capsys
):
    """Acceptance criterion #3 specifies that the script prints SUMMARY
    COUNTS (total rows, bad currency, bad exchange, bad suffix, log
    success flag) for debugging. This test goes beyond the existing
    passing test (which only checks the labels appear) and asserts the
    printed numeric values actually match the seeded DB state end-to-end
    through verify_migration.sql + verify_migration.py.

    Seeds: 4 fully-normalized stocks + 1 SUCCESS row + 1 FAILED row
           (FAILED is irrelevant to success but proves the script doesn't
            count FAILED rows as 'success').
    Expects exit_code == 0 and stdout values: total_rows=4, all bad_*=0,
    migration_log.success=1.
    """
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_normalized(conn, ["AAPL", "MSFT", "GOOGL", "AMZN"])
        _log_success(conn)
        _log_failed(conn)  # must NOT be counted as success

    exit_code = verify_migration()
    captured = capsys.readouterr()

    assert exit_code == EXIT_PASS, (
        f"expected EXIT_PASS (0) for fully-normalized stocks with a "
        f"SUCCESS log row; got exit_code={exit_code}; stdout={captured.out!r}"
    )

    # Every printed count line must carry the correct numeric value for
    # the seeded state. Use whole-line equality so a stray extra char
    # in the formatting fails loudly.
    assert "stocks.total_rows     = 4" in captured.out, (
        f"expected total_rows=4 in summary; got stdout={captured.out!r}"
    )
    assert "stocks.bad_currency   = 0" in captured.out, (
        f"expected bad_currency=0 in summary; got stdout={captured.out!r}"
    )
    assert "stocks.bad_exchange   = 0" in captured.out, (
        f"expected bad_exchange=0 in summary; got stdout={captured.out!r}"
    )
    assert "stocks.bad_suffix     = 0" in captured.out, (
        f"expected bad_suffix=0 in summary; got stdout={captured.out!r}"
    )
    # log_success_flag must be 1, NOT the row count -- this proves the
    # wrapper computes the flag from log['log_success'] > 0, not raw
    # log_total which would be 2 (SUCCESS + FAILED).
    assert "migration_log.success = 1" in captured.out, (
        f"expected migration_log.success=1 in summary; got stdout={captured.out!r}"
    )


def test_verify_migration_does_not_modify_data(
    stocks_and_log_tables,
):
    """Acceptance criterion #4: the script must be SELECT-only. Capture
    every row count and hash-equivalent signature of `stocks` and
    `migration_log` before running, run verify_migration, and assert
    nothing changed."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_normalized(conn, ["AAPL", "MSFT"])
        _seed_one_bad_row(conn)  # this leaves BAD/EUR -- a failing scenario
        _log_failed(conn)

        # Capture the exact rows present in both tables.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, currency, exchange, symbol_suffix FROM stocks ORDER BY id"
            )
            stocks_before = cur.fetchall()
            cur.execute(
                "SELECT migration_name, status, rows_affected, dry_run, error_message "
                "FROM migration_log ORDER BY id"
            )
            log_before = cur.fetchall()

    # Run verify_migration -- this should fail (BAD/EUR row) but never
    # write anything.
    exit_code = verify_migration()
    assert exit_code == EXIT_FAIL, (
        "BAD/EUR row must trigger non-zero exit"
    )

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, currency, exchange, symbol_suffix FROM stocks ORDER BY id"
            )
            stocks_after = cur.fetchall()
            cur.execute(
                "SELECT migration_name, status, rows_affected, dry_run, error_message "
                "FROM migration_log ORDER BY id"
            )
            log_after = cur.fetchall()

    assert stocks_after == stocks_before, (
        "verify_migration must not modify any row in stocks"
    )
    assert log_after == log_before, (
        "verify_migration must not modify any row in migration_log"
    )


# ---------------------------------------------------------------------------
# STORY-13: core-domain table checks
# ---------------------------------------------------------------------------


def _assert_core_domain_tables_pass(captured) -> None:
    """Shared assertions for a fully-passing core-domain check scenario.

    After the fixture has created all four tables with correct DDL,
    verify_migration must emit exists=1, pk=present, and indexes=present
    for each table. FKs are absent because no FKs were added in the
    fixture (no parent-child relationships were seeded), and since
    orphan_count=0, the FK-absent case must be reported as absent
    (not as an error, because the migration itself would have skipped
    adding them in the same situation).
    """
    for table in ("users", "portfolios", "holdings", "transactions"):
        assert f"{table}.exists = 1" in captured.out, (
            f"expected {table}.exists = 1; stdout={captured.out!r}"
        )
        assert f"{table}.pk = present" in captured.out, (
            f"expected {table}.pk = present; stdout={captured.out!r}"
        )
    # Both holdings indexes present
    assert "holdings.indexes = all present (2)" in captured.out
    # FKs: absent (no rows, no FKs added) but no failure lines
    assert "FK fk_portfolios_user: ABSENT (0 orphan" in captured.out
    assert "FK fk_holdings_portfolio: ABSENT (0 orphan" in captured.out
    assert "FK fk_transactions_portfolio: ABSENT (0 orphan" in captured.out
    # No failure lines
    assert "FAIL:" not in captured.err


def test_verify_migration_core_domain_passes_with_correct_tables(
    stocks_and_log_tables, capsys
):
    """STORY-13 acceptance: when all four core-domain tables exist with
    correct DDL (PK on id, correct columns, correct indexes), the script
    exits 0 and prints per-table / per-FK summary lines."""
    _log_success(psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True))
    # stocks_and_log_tables already created all four core-domain tables.
    exit_code = verify_migration()
    captured = capsys.readouterr()
    assert exit_code == EXIT_PASS, (
        f"expected EXIT_PASS (0) when all four tables exist with correct "
        f"DDL; got exit_code={exit_code}; stdout={captured.out!r}; "
        f"stderr={captured.err!r}"
    )
    _assert_core_domain_tables_pass(captured)


def test_verify_migration_core_domain_fails_with_missing_table(
    stocks_and_log_tables, capsys
):
    """STORY-13 acceptance: when any of the four core-domain tables is
    absent, the script exits non-zero and names the missing table in
    its output."""
    # Drop one table after the fixture has set everything up.
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE holdings")
        _log_success(conn)

    exit_code = verify_migration()
    captured = capsys.readouterr()

    assert exit_code == EXIT_FAIL, (
        f"expected EXIT_FAIL (1) when holdings table is absent; "
        f"got exit_code={exit_code}; stdout={captured.out!r}; "
        f"stderr={captured.err!r}"
    )
    # The missing table name must appear in the output.
    assert "holdings" in captured.out or "holdings" in captured.err, (
        f"expected 'holdings' to be named in output when table is missing; "
        f"stdout={captured.out!r}; stderr={captured.err!r}"
    )


def test_verify_migration_core_domain_checks_columns_and_types(
    stocks_and_log_tables, capsys
):
    """STORY-13 acceptance: the script detects a column that is missing,
    has the wrong type, or has the wrong nullability."""
    # Add a column with a wrong type (TEXT instead of JSONB) to users.preferences.
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE users DROP COLUMN preferences")
            cur.execute("ALTER TABLE users ADD COLUMN preferences TEXT DEFAULT '{}'")
        _log_success(conn)

    exit_code = verify_migration()
    captured = capsys.readouterr()

    assert exit_code == EXIT_FAIL, (
        f"expected EXIT_FAIL (1) when users.preferences has wrong type; "
        f"got exit_code={exit_code}"
    )
    assert "FAIL:" in captured.err, (
        f"expected a FAIL: line for wrong column type; stderr={captured.err!r}"
    )


def test_verify_migration_core_domain_reports_fk_absent_with_orphans(
    stocks_and_log_tables, capsys
):
    """STORY-13 acceptance: when a FK is absent but orphan rows exist,
    the script reports it as ABSENT (not as a failure) with the orphan
    count, per the STORY-12 design where FKs are intentionally skipped
    when legacy orphan rows are present."""
    # Create a portfolio with a user_id that does not exist in users.
    # This makes the FK fk_portfolios_user impossible to add.
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolios (id, user_id) VALUES ('orphan-pf', 'ghost-user')"
            )
        _log_success(conn)

    exit_code = verify_migration()
    captured = capsys.readouterr()

    assert exit_code == EXIT_PASS, (
        f"FK absent with orphans must NOT fail verification; "
        f"got exit_code={exit_code}; stdout={captured.out!r}; "
        f"stderr={captured.err!r}"
    )
    assert "FK fk_portfolios_user: ABSENT (1 orphan row(s)" in captured.out, (
        f"expected ABSENT with orphan count; stdout={captured.out!r}"
    )
    assert "FAIL:" not in captured.err, (
        f"FK absent with orphans must not be a failure; stderr={captured.err!r}"
    )


def test_verify_migration_core_domain_fk_absent_zero_orphans_fails(
    stocks_and_log_tables, capsys
):
    """STORY-13 acceptance: when a FK is absent but orphan_count == 0,
    the script must exit non-zero and report it as a failure, because
    the FK should have been added by the migration and wasn't.

    This differs from the orphan>0 case which is permitted by design
    (STORY-12 deliberately skips FKs when orphan rows exist)."""
    # The fixture created tables but no FKs.  Since there are also no
    # orphan rows (the tables are empty), fk_portfolios_user should
    # have been added by the migration and is missing → FAIL.
    _log_success(psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True))

    exit_code = verify_migration()
    captured = capsys.readouterr()

    assert exit_code == EXIT_FAIL, (
        f"FK absent with 0 orphans must exit non-zero; "
        f"got exit_code={exit_code}; stdout={captured.out!r}; "
        f"stderr={captured.err!r}"
    )
    assert "FK fk_portfolios_user: ABSENT but 0 orphan rows" in captured.err, (
        f"expected FAIL line for absent FK with 0 orphans; "
        f"stderr={captured.err!r}"
    )
