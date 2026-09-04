"""QA verification for STORY-5: idempotency, atomicity, zero data loss.

This file is the QA agent's independent verification. Each of the four
acceptance criteria from STORY-5 is exercised by exactly one focused,
self-contained test against the live Postgres that the rest of the
integration tests in this repo use.

CI-pipeline-integration is intentionally not covered here — issue #76
owns that, per the story description.

The schema for migration_log lives in src/infrastructure_postgres.py
(DefaultInfrastructure._ensure_schema). The migration script
(scripts/migrate_us_stocks.py) references it by name and writes one
row per real invocation. We use DefaultInfrastructure().store(...) to
lazily create the migration_log table on demand; that matches the
established pattern in tests/test_infrastructure_postgres.py and
issue #71's verification script.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from infrastructure_postgres import DEFAULT_POSTGRES_DSN, DefaultInfrastructure
from scripts.migrate_us_stocks import MIGRATION_NAME, migrate_us_stocks


# ---------------------------------------------------------------------------
# Shared infrastructure
# ---------------------------------------------------------------------------


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=1):
            return True
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="no live Postgres reachable at DEFAULT_POSTGRES_DSN",
)


def _bootstrap_migration_log_table() -> None:
    """Force DefaultInfrastructure._ensure_schema to create migration_log
    (and its index) on demand. DefaultInfrastructure is idempotent on
    schema creation, so this is safe to call repeatedly."""
    DefaultInfrastructure().store(
        f"qa_story5_seed_{uuid.uuid4().hex}",
        {"id": "seed", "x": 1},
    )


def _migration_log_baseline() -> int:
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM migration_log")
            return int(cur.fetchone()[0])


def _log_rows_since(baseline: int) -> list[tuple]:
    """Return migration_log rows scoped to MIGRATION_NAME + id > baseline,
    as (status, rows_affected, error_message, dry_run)."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, rows_affected, error_message, dry_run "
                "FROM migration_log "
                "WHERE migration_name = %s AND id > %s "
                "ORDER BY id ASC",
                (MIGRATION_NAME, baseline),
            )
            return cur.fetchall()


@pytest.fixture
def stocks_table():
    """Create the canonical stocks + tmp_us_tickers pair, drop on teardown.
    Mirrors the existing tests/test_migrate_us_stocks.py fixture."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE stocks (
                    id TEXT PRIMARY KEY,
                    currency TEXT,
                    exchange TEXT,
                    symbol_suffix TEXT
                )
                """
            )
            cur.execute("CREATE TABLE tmp_us_tickers (id TEXT PRIMARY KEY)")

    yield "stocks", "tmp_us_tickers"

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS stocks")
            cur.execute("DROP TABLE IF EXISTS tmp_us_tickers")


# ---------------------------------------------------------------------------
# AC #1: First run -> differing rows normalized, migration_log SUCCESS,
#         rows_affected > 0.
# ---------------------------------------------------------------------------


def test_ac1_first_run_normalizes_target_rows_and_logs_success(
    stocks_table, monkeypatch
):
    """AC #1: After first run, target rows have correct defaults and
    migration_log shows SUCCESS with rows_affected > 0."""
    _bootstrap_migration_log_table()
    baseline = _migration_log_baseline()

    # Seed: AAPL=correct, MSFT=currency wrong, GOOGL=exchange+suffix wrong,
    # TSLA=non-target (must never be touched).
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO tmp_us_tickers (id) VALUES (%s)",
                [("AAPL",), ("MSFT",), ("GOOGL",)],
            )
            cur.executemany(
                "INSERT INTO stocks (id, currency, exchange, symbol_suffix) "
                "VALUES (%s, %s, %s, %s)",
                [
                    ("AAPL", "USD", None, None),
                    ("MSFT", "EUR", None, None),
                    ("GOOGL", "USD", "NASDAQ", ".OLD"),
                    ("TSLA", "EUR", "XETRA", ".F"),
                ],
            )

    monkeypatch.delenv("MIGRATION_DRY_RUN", raising=False)
    rows = migrate_us_stocks()

    # Two rows differ from canonical (MSFT, GOOGL); AAPL was already correct.
    assert rows == 2, f"first run must update exactly 2 rows; got {rows}"
    assert rows > 0, "AC #1 requires rows_affected > 0 on first run"

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, currency, exchange, symbol_suffix FROM stocks ORDER BY id"
            )
            rows_db = {r[0]: r[1:] for r in cur.fetchall()}

    # All three targets now match canonical (USD, NULL, NULL).
    assert rows_db["AAPL"] == ("USD", None, None)
    assert rows_db["MSFT"] == ("USD", None, None), (
        f"MSFT currency must be normalized to USD; got {rows_db['MSFT']}"
    )
    assert rows_db["GOOGL"] == ("USD", None, None), (
        f"GOOGL exchange + suffix must be cleared; got {rows_db['GOOGL']}"
    )
    # TSLA is not in tmp_us_tickers; must never be touched.
    assert rows_db["TSLA"] == ("EUR", "XETRA", ".F"), (
        f"TSLA must be untouched; got {rows_db['TSLA']}"
    )

    # migration_log must contain exactly one new SUCCESS row for this
    # migration, with rows_affected matching the UPDATE rowcount.
    log = _log_rows_since(baseline)
    assert len(log) == 1, (
        f"first run must log exactly one row to migration_log; got {log}"
    )
    status, rows_affected, error_message, dry_run = log[0]
    assert status == "SUCCESS", f"first run status must be SUCCESS; got {status!r}"
    assert int(rows_affected) == 2, (
        f"rows_affected must equal the UPDATE rowcount (2); got {rows_affected!r}"
    )
    assert error_message is None, (
        f"SUCCESS rows must have NULL error_message; got {error_message!r}"
    )
    assert dry_run is False, f"real run must log dry_run=False; got {dry_run!r}"


# ---------------------------------------------------------------------------
# AC #2: Second run -> rows_affected = 0, logs SUCCESS.
# ---------------------------------------------------------------------------


def test_ac2_second_run_is_no_op_but_still_logs_success(
    stocks_table, monkeypatch
):
    """AC #2: Second run, no data changes, reports rows_affected = 0 and
    still logs SUCCESS."""
    _bootstrap_migration_log_table()
    baseline = _migration_log_baseline()

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO tmp_us_tickers (id) VALUES (%s)",
                [("AAPL",), ("MSFT",), ("GOOGL",)],
            )
            cur.executemany(
                "INSERT INTO stocks (id, currency, exchange, symbol_suffix) "
                "VALUES (%s, %s, %s, %s)",
                [
                    ("AAPL", "USD", None, None),
                    ("MSFT", "EUR", None, None),
                    ("GOOGL", "USD", "NASDAQ", ".OLD"),
                    ("TSLA", "EUR", "XETRA", ".F"),
                ],
            )

    monkeypatch.delenv("MIGRATION_DRY_RUN", raising=False)

    first = migrate_us_stocks()
    assert first == 2

    # Second real run: data is already normalized, must report 0 changes.
    second = migrate_us_stocks()
    assert second == 0, f"second real run must report rows_affected=0; got {second}"

    log = _log_rows_since(baseline)
    assert len(log) == 2, (
        f"two real runs must log exactly two rows; got {len(log)}: {log}"
    )
    # First: SUCCESS, rows_affected = 2.
    assert log[0][0] == "SUCCESS"
    assert int(log[0][1]) == 2
    # Second: SUCCESS, rows_affected = 0 (idempotent no-op).
    assert log[1][0] == "SUCCESS", (
        f"second run status must still be SUCCESS; got {log[1][0]!r}"
    )
    assert int(log[1][1]) == 0, (
        f"second run rows_affected must be 0; got {log[1][1]!r}"
    )
    assert log[1][3] is False, (
        f"second run must log dry_run=False; got {log[1][3]!r}"
    )


# ---------------------------------------------------------------------------
# AC #3: Error mid-migration -> rollback, stocks unchanged, FAILED row with
#         a real error_message, exception propagated.
# ---------------------------------------------------------------------------


def test_ac3_error_mid_migration_rolls_back_logs_failed_and_reraises(
    stocks_table, monkeypatch
):
    """AC #3: An error mid-migration causes the (single-statement,
    autocommit) transaction to roll back; stocks is unchanged;
    migration_log gets a FAILED row with a real error_message; the
    underlying exception is propagated."""
    _bootstrap_migration_log_table()
    baseline = _migration_log_baseline()

    # Build a deliberately broken stocks schema (one column only) so the
    # real UPDATE raises a column-reference error against this table.
    # tmp_us_tickers is created normally so the UPDATE can still parse
    # and reach the column lookup, exercising the real code path.
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS stocks")
            cur.execute("CREATE TABLE stocks (id TEXT PRIMARY KEY)")
            cur.executemany(
                "INSERT INTO tmp_us_tickers (id) VALUES (%s)",
                [("AAPL",), ("MSFT",)],
            )
            cur.executemany(
                "INSERT INTO stocks (id) VALUES (%s)",
                [("AAPL",), ("MSFT",)],
            )
            # Snapshot stocks schema + rows before the failing migration.
            cur.execute("SELECT id FROM stocks ORDER BY id")
            ids_before = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'stocks' ORDER BY ordinal_position"
            )
            cols_before = [r[0] for r in cur.fetchall()]

    monkeypatch.delenv("MIGRATION_DRY_RUN", raising=False)

    # The real UPDATE must raise -- the story requires the error to
    # propagate out of migrate_us_stocks(), not be silently swallowed.
    with pytest.raises(psycopg.Error) as excinfo:
        migrate_us_stocks()

    # The raised error must come from the real SQL path: a missing-column
    # error referencing one of the columns the UPDATE targets.
    msg = str(excinfo.value).lower()
    assert "currency" in str(excinfo.value) or "column" in msg, (
        f"raised error must reference a missing column; got {excinfo.value!r}"
    )

    # stocks must be unchanged: same row ids, same columns, same row count.
    # The single-statement autocommit transaction rolled back the failed
    # UPDATE automatically before the exception was caught and logged.
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM stocks ORDER BY id")
            ids_after = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'stocks' ORDER BY ordinal_position"
            )
            cols_after = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT count(*) FROM stocks")
            row_count = int(cur.fetchone()[0])

    assert ids_after == ids_before, (
        f"failed migration must not change stocks row ids; "
        f"before={ids_before} after={ids_after}"
    )
    assert cols_after == cols_before, (
        f"failed migration must not change stocks schema; "
        f"before={cols_before} after={cols_after}"
    )
    assert row_count == 2, f"failed migration must not add/drop rows; got {row_count}"

    # migration_log must contain exactly one FAILED row with a real
    # error_message referencing the column error, rows_affected NULL.
    log = _log_rows_since(baseline)
    assert len(log) == 1, (
        f"failed run must log exactly one row to migration_log; got {log}"
    )
    status, rows_affected, error_message, dry_run = log[0]
    assert status == "FAILED", f"failed run must log status=FAILED; got {status!r}"
    assert rows_affected is None, (
        f"FAILED rows must have NULL rows_affected; got {rows_affected!r}"
    )
    assert error_message is not None and error_message != "", (
        f"FAILED rows must record a real error_message; got {error_message!r}"
    )
    assert "currency" in error_message or "column" in error_message.lower(), (
        f"error_message must reference the underlying column error; got "
        f"{error_message!r}"
    )
    assert dry_run is False, (
        f"failure was on real UPDATE path, not dry-run; got dry_run={dry_run!r}"
    )


# ---------------------------------------------------------------------------
# AC #4: Dry-run -> stocks unchanged, DRY_RUN row logged.
# ---------------------------------------------------------------------------


def test_ac4_dry_run_leaves_stocks_unchanged_and_logs_dry_run(
    stocks_table, monkeypatch
):
    """AC #4: Dry-run mode leaves stocks unchanged and logs DRY_RUN."""
    _bootstrap_migration_log_table()
    baseline = _migration_log_baseline()

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO tmp_us_tickers (id) VALUES (%s)",
                [("AAPL",), ("MSFT",), ("GOOGL",)],
            )
            cur.executemany(
                "INSERT INTO stocks (id, currency, exchange, symbol_suffix) "
                "VALUES (%s, %s, %s, %s)",
                [
                    ("AAPL", "USD", None, None),
                    ("MSFT", "EUR", None, None),
                    ("GOOGL", "USD", "NASDAQ", ".OLD"),
                    ("TSLA", "EUR", "XETRA", ".F"),
                ],
            )
            # Snapshot the entire stocks table once, so we can prove no
            # row ever changes across any number of dry-run invocations.
            cur.execute(
                "SELECT id, currency, exchange, symbol_suffix FROM stocks "
                "ORDER BY id"
            )
            before = [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]

    monkeypatch.setenv("MIGRATION_DRY_RUN", "true")

    first = migrate_us_stocks()
    assert first == 2, (
        f"dry-run must report the 2 rows that would change; got {first}"
    )

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, currency, exchange, symbol_suffix FROM stocks ORDER BY id"
            )
            after_first = [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]

    assert after_first == before, (
        f"dry-run must not modify any row; before={before} after={after_first}"
    )

    # Second dry-run -- still must not modify anything, still reports 2.
    second = migrate_us_stocks()
    assert second == 2, (
        f"a second dry-run must still report 2 would-change rows; got {second}"
    )

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, currency, exchange, symbol_suffix FROM stocks ORDER BY id"
            )
            after_second = [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]

    assert after_second == before, (
        f"second dry-run must also leave stocks unchanged; "
        f"before={before} after={after_second}"
    )
    # The deliberately-wrong pre-normalization values must still be there.
    msft = next(row for row in after_second if row[0] == "MSFT")
    assert msft[1] == "EUR", (
        f"MSFT must retain its pre-normalization wrong currency after dry-run; "
        f"got {msft[1]!r}"
    )
    googl = next(row for row in after_second if row[0] == "GOOGL")
    assert googl[2] == "NASDAQ" and googl[3] == ".OLD", (
        f"GOOGL must retain its pre-normalization exchange + suffix after "
        f"dry-run; got exchange={googl[2]!r} suffix={googl[3]!r}"
    )

    # migration_log must contain two new DRY_RUN rows for this test, both
    # with dry_run=True and rows_affected=2.
    log = _log_rows_since(baseline)
    assert len(log) == 2, (
        f"two dry-runs must log exactly two rows; got {len(log)}: {log}"
    )
    for i, row in enumerate(log):
        status, rows_affected, error_message, dry_run = row
        assert status == "DRY_RUN", (
            f"dry-run row {i} must have status=DRY_RUN; got {status!r}"
        )
        assert int(rows_affected) == 2, (
            f"dry-run row {i} rows_affected must be 2; got {rows_affected!r}"
        )
        assert error_message is None, (
            f"DRY_RUN rows must have NULL error_message; got {error_message!r}"
        )
        assert dry_run is True, (
            f"dry-run row {i} must set dry_run=True; got {dry_run!r}"
        )