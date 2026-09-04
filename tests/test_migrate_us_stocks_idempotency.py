"""Tests for STORY-5: idempotency, atomicity, and zero data loss.

These tests verify the four behaviors the story commits to:

  1. First real run normalizes the differing rows and logs a SUCCESS row
     in migration_log with rows_affected > 0.
  2. Second real run, with data already normalized, reports
     rows_affected = 0 and still logs SUCCESS (idempotency).
  3. If an error is introduced mid-migration (here: the real UPDATE
     targets a stocks table missing a required column), the
     single-statement autocommit transaction rolls back automatically,
     stocks is left unchanged, migration_log gets a FAILED row with a
     real error_message, and the underlying psycopg exception is
     re-raised by migrate_us_stocks().
  4. Dry-run mode leaves stocks completely unchanged and logs DRY_RUN.

The CI-pipeline-integration part of the story's acceptance criteria is
owned by issue #76; this file only exercises the in-process Python
behaviour, against the same live Postgres the rest of the integration
tests in this repo use.

migration_log is a real shared table that other tests (and issue #74's
verification script) also write to, so every test scopes its assertions
by migration_name = MIGRATION_NAME and by ordering of row insertion
(ranking) to remain stable regardless of test interleaving.
"""

from __future__ import annotations

import pytest
import psycopg

from infrastructure_postgres import (
    DEFAULT_POSTGRES_DSN,
    DefaultInfrastructure,
)

from scripts.migrate_us_stocks import MIGRATION_NAME, migrate_us_stocks


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=1):
            return True
    except psycopg.Error:
        return False


POSTGRES_SKIP_REASON = (
    "no live Postgres reachable at DEFAULT_POSTGRES_DSN — "
    "run `docker-compose up -d` for real coverage"
)
pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason=POSTGRES_SKIP_REASON
)


# ---------------------------------------------------------------------------
# Shared fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def stocks_table():
    """Create the canonical stocks + tmp_us_tickers pair, drop on teardown.

    Mirrors tests/test_migrate_us_stocks.py's fixture so the SQL in
    scripts/migrate_us_stocks.sql (which targets stocks.id, .currency,
    .exchange, .symbol_suffix joined to tmp_us_tickers.id) runs cleanly.
    """
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


def _seed_three_differing_one_correct_one_untouched(conn) -> dict[str, tuple]:
    """Seed exactly the fixture STORY-5 needs:

      * AAPL  — already normalized (USD, NULL exchange, NULL suffix)
      * MSFT  — wrong currency only
      * GOOGL — exchange + suffix set, currency already USD
      * TSLA  — NOT in tmp_us_tickers; must never be touched

    Returns a mapping id -> (currency, exchange, suffix) of the seeded
    values so tests can compare before/after deterministically.
    """
    seeded = {
        "AAPL": ("USD", None, None),
        "MSFT": ("EUR", None, None),
        "GOOGL": ("USD", "NASDAQ", ".OLD"),
        "TSLA": ("EUR", "XETRA", ".F"),
    }
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO tmp_us_tickers (id) VALUES (%s)",
            [("AAPL",), ("MSFT",), ("GOOGL",)],
        )
        cur.executemany(
            "INSERT INTO stocks (id, currency, exchange, symbol_suffix) "
            "VALUES (%s, %s, %s, %s)",
            [(tid, *vals) for tid, vals in seeded.items()],
        )
    return seeded


def _read_all(conn, ids: list[str]) -> dict[str, tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, currency, exchange, symbol_suffix FROM stocks "
            "WHERE id = ANY(%s)",
            (ids,),
        )
        return {row[0]: row[1:] for row in cur.fetchall()}


def _migration_log_baseline() -> int:
    """Return the current maximum id in migration_log so tests can isolate
    only the rows they themselves inserted (the table is shared with
    every other test in the suite)."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM migration_log")
            return int(cur.fetchone()[0])


def _migration_log_since(baseline: int) -> list[tuple]:
    """Return migration_log rows for MIGRATION_NAME with id > baseline,
    in insertion order, as (status, rows_affected, error_message, dry_run)."""
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


def _ensure_migration_log_table() -> None:
    """Touch DefaultInfrastructure so its lazy _ensure_schema creates the
    migration_log table (and its index) before any test reads from it.
    DefaultInfrastructure.store() is the simplest public way to trigger
    schema creation without doing real work; the table name is namespaced
    with a uuid so it doesn't collide with anything else."""
    import uuid as _uuid

    DefaultInfrastructure().store(
        f"test_migration_log_seed_{_uuid.uuid4().hex}",
        {"id": "seed", "x": 1},
    )


# ---------------------------------------------------------------------------
# Test 1: first real run updates the differing rows and logs SUCCESS
# ---------------------------------------------------------------------------


def test_first_real_run_updates_differing_rows_and_logs_success(
    stocks_table, monkeypatch
):
    """AC #1: after the first real run, all target rows have the correct
    defaults and migration_log shows SUCCESS with rows_affected > 0."""
    _ensure_migration_log_table()
    baseline = _migration_log_baseline()

    target_ids = ["AAPL", "MSFT", "GOOGL"]
    all_ids = target_ids + ["TSLA"]

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        seeded = _seed_three_differing_one_correct_one_untouched(conn)

    # Sanity: TSLA was not in tmp_us_tickers; preserve its non-target
    # baseline so we can prove it is never modified.
    tsla_before = seeded["TSLA"]

    # Real run, env var not set explicitly — must default to a real run.
    monkeypatch.delenv("MIGRATION_DRY_RUN", raising=False)
    rows = migrate_us_stocks()

    # MSFT (currency wrong) and GOOGL (exchange + suffix wrong) differ;
    # AAPL was already correct; TSLA isn't a target.
    assert rows == 2, f"first real run should update exactly 2 rows; got {rows}"

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        after = _read_all(conn, all_ids)

    # All three target rows now have the canonical normalized values.
    assert after["AAPL"] == ("USD", None, None), (
        f"AAPL was already correct and must be unchanged; got {after['AAPL']}"
    )
    assert after["MSFT"] == ("USD", None, None), (
        f"MSFT currency must be normalized to USD; got {after['MSFT']}"
    )
    assert after["GOOGL"] == ("USD", None, None), (
        f"GOOGL exchange + suffix must be cleared; got {after['GOOGL']}"
    )
    # TSLA must be untouched — it's not in tmp_us_tickers.
    assert after["TSLA"] == tsla_before, (
        f"TSLA is not a US target and must be untouched; "
        f"before={tsla_before} after={after['TSLA']}"
    )

    # migration_log must contain exactly one new row for this migration,
    # with status SUCCESS and rows_affected matching the rowcount above.
    rows_logged = _migration_log_since(baseline)
    assert len(rows_logged) == 1, (
        f"first real run must log exactly one row to migration_log; got "
        f"{len(rows_logged)}: {rows_logged}"
    )
    status, rows_affected, error_message, dry_run = rows_logged[0]
    assert status == "SUCCESS", f"first run status must be SUCCESS; got {status!r}"
    assert int(rows_affected) == 2, (
        f"migration_log rows_affected must match the UPDATE rowcount (2); "
        f"got {rows_affected!r}"
    )
    assert int(rows_affected) > 0, "AC requires rows_affected > 0 on first run"
    assert error_message is None, (
        f"SUCCESS rows must have NULL error_message; got {error_message!r}"
    )
    assert dry_run is False, "real run must log dry_run=False"


# ---------------------------------------------------------------------------
# Test 2: second real run with no data changes is a no-op + still logs
# ---------------------------------------------------------------------------


def test_second_real_run_is_no_op_and_still_logs_success(
    stocks_table, monkeypatch
):
    """AC #2: a second real run with no intervening data changes reports
    rows_affected = 0 and still logs SUCCESS (idempotent)."""
    _ensure_migration_log_table()
    baseline = _migration_log_baseline()

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_three_differing_one_correct_one_untouched(conn)
        snapshot_before_second_run = _read_all(
            conn, ["AAPL", "MSFT", "GOOGL", "TSLA"]
        )

    monkeypatch.delenv("MIGRATION_DRY_RUN", raising=False)

    # First real run normalizes the data.
    first = migrate_us_stocks()
    assert first == 2, f"first run must update 2 rows; got {first}"

    # Snapshot the post-first-run state to prove the second run is a
    # genuine no-op against it.
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        snapshot_after_first_run = _read_all(
            conn, ["AAPL", "MSFT", "GOOGL", "TSLA"]
        )

    # Second real run — every target row already matches the canonical
    # values, so the WHERE clause matches nothing.
    second = migrate_us_stocks()
    assert second == 0, (
        f"second real run must report rows_affected = 0; got {second}"
    )

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        snapshot_after_second_run = _read_all(
            conn, ["AAPL", "MSFT", "GOOGL", "TSLA"]
        )

    # The second run must not have touched any row.
    assert snapshot_after_second_run == snapshot_after_first_run, (
        "second real run must leave every row identical to post-first-run "
        f"state; before={snapshot_after_first_run} after={snapshot_after_second_run}"
    )
    # And the post-first-run state must already be fully normalized
    # (no data loss between runs).
    assert snapshot_after_first_run["AAPL"] == ("USD", None, None)
    assert snapshot_after_first_run["MSFT"] == ("USD", None, None)
    assert snapshot_after_first_run["GOOGL"] == ("USD", None, None)
    assert snapshot_after_first_run["TSLA"] == snapshot_before_second_run["TSLA"], (
        "TSLA must remain untouched after the first real run"
    )

    # migration_log must contain two new SUCCESS rows since baseline.
    rows_logged = _migration_log_since(baseline)
    assert len(rows_logged) == 2, (
        f"two real runs must log exactly two rows; got {len(rows_logged)}: "
        f"{rows_logged}"
    )
    # First: SUCCESS, rows_affected = 2.
    assert rows_logged[0][0] == "SUCCESS"
    assert int(rows_logged[0][1]) == 2
    # Second: SUCCESS, rows_affected = 0 (idempotent no-op).
    assert rows_logged[1][0] == "SUCCESS", (
        f"second run status must still be SUCCESS; got {rows_logged[1][0]!r}"
    )
    assert int(rows_logged[1][1]) == 0, (
        f"second run rows_affected must be 0; got {rows_logged[1][1]!r}"
    )
    assert rows_logged[1][3] is False, "second run must log dry_run=False"


# ---------------------------------------------------------------------------
# Test 3: an error mid-migration rolls back and logs FAILED with the message
# ---------------------------------------------------------------------------


def test_error_mid_migration_rolls_back_logs_failed_and_reraises(
    stocks_table, monkeypatch
):
    """AC #3: when the real UPDATE raises (here because stocks is missing
    the columns the SQL targets), the single-statement autocommit
    transaction rolls back automatically, stocks is left unchanged,
    migration_log gets a FAILED row with a real error_message, and the
    exception is re-raised by migrate_us_stocks()."""
    _ensure_migration_log_table()
    baseline = _migration_log_baseline()

    # Seed a stocks table that is *deliberately broken*: it has only `id`.
    # The migration's UPDATE references currency, exchange, and
    # symbol_suffix, so psycopg raises UndefinedColumn against this
    # table -- exactly the kind of mid-migration error the story covers.
    #
    # tmp_us_tickers is created normally so the failing UPDATE can still
    # parse and reach the column lookup, exercising the real code path.
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Recreate stocks as a minimal one-column table, replacing the
            # stocks_table fixture's full schema. We do NOT drop the
            # canonical fixture here -- this is the only test that needs
            # the broken schema, so we override the table in place.
            cur.execute("DROP TABLE IF EXISTS stocks")
            cur.execute(
                "CREATE TABLE stocks (id TEXT PRIMARY KEY)"
            )
            cur.execute(
                "INSERT INTO tmp_us_tickers (id) VALUES (%s)",
                ("AAPL",),
            )
            cur.execute(
                "INSERT INTO stocks (id) VALUES (%s)",
                ("AAPL",),
            )

        # Snapshot stocks before the failing migration; the row must
        # remain identical afterwards.
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM stocks ORDER BY id")
            ids_before = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'stocks' ORDER BY ordinal_position"
            )
            cols_before = [r[0] for r in cur.fetchall()]

    monkeypatch.delenv("MIGRATION_DRY_RUN", raising=False)

    # The real UPDATE must raise -- the story requires the error to
    # propagate, not be silently swallowed.
    with pytest.raises(psycopg.Error) as excinfo:
        migrate_us_stocks()

    # The underlying error must come from the real SQL path (a column
    # lookup against the missing columns), not from something unrelated.
    assert excinfo.value is not None
    assert "currency" in str(excinfo.value) or "column" in str(excinfo.value).lower(), (
        f"raised error must reference the missing column; got {excinfo.value!r}"
    )

    # stocks must be unchanged: same row ids, same columns. Because each
    # statement runs in its own autocommit transaction, the failed
    # UPDATE was rolled back automatically by psycopg before the
    # exception was caught and logged.
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM stocks ORDER BY id")
            ids_after = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'stocks' ORDER BY ordinal_position"
            )
            cols_after = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT count(*) FROM stocks"
            )
            stock_row_count = int(cur.fetchone()[0])

    assert ids_after == ids_before, (
        f"failed migration must not change stocks row ids; "
        f"before={ids_before} after={ids_after}"
    )
    assert cols_after == cols_before, (
        f"failed migration must not change stocks schema; "
        f"before={cols_before} after={cols_after}"
    )
    assert stock_row_count == 1, (
        f"failed migration must not add or drop rows; got {stock_row_count}"
    )

    # migration_log must contain exactly one new row: status=FAILED,
    # rows_affected NULL, error_message populated with the real error,
    # dry_run=False.
    rows_logged = _migration_log_since(baseline)
    assert len(rows_logged) == 1, (
        f"failed run must log exactly one row to migration_log; got "
        f"{len(rows_logged)}: {rows_logged}"
    )
    status, rows_affected, error_message, dry_run = rows_logged[0]
    assert status == "FAILED", f"failed run must log status=FAILED; got {status!r}"
    assert rows_affected is None, (
        f"FAILED rows must have NULL rows_affected; got {rows_affected!r}"
    )
    assert error_message is not None and error_message != "", (
        "FAILED rows must record a real error_message; got "
        f"{error_message!r}"
    )
    assert "currency" in error_message or "column" in error_message.lower(), (
        f"error_message must reference the underlying column error; got "
        f"{error_message!r}"
    )
    assert dry_run is False, (
        "the failure here is on the *real* UPDATE path, not a dry-run; "
        f"got dry_run={dry_run!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: dry-run mode leaves stocks untouched and logs DRY_RUN
# ---------------------------------------------------------------------------


def test_dry_run_leaves_stocks_unchanged_and_logs_dry_run(
    stocks_table, monkeypatch
):
    """AC #4: dry-run mode issues no UPDATE and reports rows that *would*
    have changed; stocks must remain identical to the seeded state and
    migration_log must show DRY_RUN with the dry_run flag set True."""
    _ensure_migration_log_table()
    baseline = _migration_log_baseline()

    target_ids = ["AAPL", "MSFT", "GOOGL"]
    all_ids = target_ids + ["TSLA"]

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        seeded = _seed_three_differing_one_correct_one_untouched(conn)
        snapshot_before = _read_all(conn, all_ids)

    monkeypatch.setenv("MIGRATION_DRY_RUN", "true")

    # Two consecutive dry-runs to also exercise that dry-run itself is
    # idempotent and never modifies anything.
    first = migrate_us_stocks()
    assert first == 2, (
        f"dry-run must report the 2 rows that would change; got {first}"
    )

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        snapshot_after_first = _read_all(conn, all_ids)

    assert snapshot_after_first == snapshot_before, (
        "dry-run must not modify any row; "
        f"before={snapshot_before} after={snapshot_after_first}"
    )

    second = migrate_us_stocks()
    assert second == 2, (
        f"a second dry-run must still report the same 2 would-change rows; "
        f"got {second}"
    )

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        snapshot_after_second = _read_all(conn, all_ids)

    assert snapshot_after_second == snapshot_before, (
        "second dry-run must also leave stocks unchanged; "
        f"before={snapshot_before} after={snapshot_after_second}"
    )
    # And the seeded, pre-normalization values must still be intact --
    # MSFT and GOOGL were deliberately wrong on purpose.
    assert snapshot_after_second["MSFT"] == seeded["MSFT"], (
        "MSFT must retain its pre-normalization wrong currency after dry-run; "
        f"got {snapshot_after_second['MSFT']}"
    )
    assert snapshot_after_second["GOOGL"] == seeded["GOOGL"], (
        "GOOGL must retain its pre-normalization exchange + suffix after "
        f"dry-run; got {snapshot_after_second['GOOGL']}"
    )

    # migration_log must contain exactly two new DRY_RUN rows, both with
    # dry_run=True and a real rows_affected count.
    rows_logged = _migration_log_since(baseline)
    assert len(rows_logged) == 2, (
        f"two dry-runs must log exactly two rows; got {len(rows_logged)}: "
        f"{rows_logged}"
    )
    for i, row in enumerate(rows_logged):
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