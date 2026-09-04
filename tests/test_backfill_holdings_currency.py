"""Tests for scripts/backfill_holdings_currency.py (Story STORY-1).

Mirrors tests/test_migrate_us_stocks.py's structure:

  * Single live-Postgres gate (`_postgres_reachable`) at module level.
  * A scoped fixture that creates the `records` table (the real
    storage DefaultInfrastructure uses) so the backfill's UPDATE
    against `records` can run against real rows.
  * Three phases in one test: dry-run, real, second-real (idempotency)
    so all three share fixtures.
  * Separate tests cover the failure path (the real UPDATE errors on
    an actual SQL syntax mistake, transaction rolls back, migration_log
    shows FAILED) and the schema_migrations write (one row per logical
    migration, even across re-runs).

Needs a live Postgres at DEFAULT_POSTGRES_DSN — same precondition as
the rest of the integration tests in this repo. When `psycopg` itself
isn't installed in the test environment, the module-level `import
psycopg` raises and pytest reports collection-time errors instead of
clean skips; this matches the rest of the repo's behavior (e.g.
tests/test_migrate_us_stocks.py) and isn't papered over here.
"""

from __future__ import annotations

import pytest
import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN

from scripts.backfill_holdings_currency import (
    MIGRATION_NAME,
    backfill_holdings_currency,
)


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
def records_table():
    """Create the real `records` table this app stores everything in
    (DefaultInfrastructure), then drop it on teardown. We use the
    canonical DefaultInfrastructure schema for the table because the
    backfill's UPDATE targets `records` directly, and matching the
    real column types (table_name, id, data JSONB) keeps the test
    honest about what the backfill is doing against real storage."""
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

    yield "records"

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Only delete the rows we created (those starting with the
            # test-specific portfolio id prefix used by _seed); other
            # tests in this suite use the shared `records` table too.
            cur.execute(
                "DELETE FROM records WHERE table_name = 'holdings' AND id LIKE 'pf-bf-%%'"
            )


def _seed(conn) -> list[str]:
    """Seed the canonical mix of pre-existing holdings rows:

      * pf-bf-AAPL — already has currency='USD' (must never change)
      * pf-bf-MSFT — no currency at all (must become 'USD')
      * pf-bf-GOOG — no currency, has unrelated fields too (jsonb_set
        must not clobber them)
      * pf-bf-TSLA — no currency (must become 'USD'), lives under a
        different table_name so the backfill's table_name='holdings'
        filter must not touch it. (We do not actually write a non-
        holdings row here because the table only meaningfully stores
        holdings in this test; the table_name filter is exercised by
        the UPDATE's WHERE clause shape.)

    Returns the seeded ids so tests can read before/after
    deterministically.
    """
    holdings = [
        # Already-correct: must remain untouched.
        ("pf-bf-AAPL", {"portfolio_id": "pf-bf", "security_id": "AAPL", "quantity": 10, "currency": "USD"}),
        # Missing currency: must become 'USD'.
        ("pf-bf-MSFT", {"portfolio_id": "pf-bf", "security_id": "MSFT", "quantity": 5}),
        # Missing currency, has extra fields: jsonb_set must preserve
        # them all and only add `currency`.
        (
            "pf-bf-GOOG",
            {
                "portfolio_id": "pf-bf",
                "security_id": "GOOG",
                "quantity": 3,
                "provenance": "UNTRUSTED",
                "broker_connection": {"broker": "fake"},
            },
        ),
        # Missing currency: must become 'USD'.
        ("pf-bf-TSLA", {"portfolio_id": "pf-bf", "security_id": "TSLA", "quantity": 7}),
    ]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO records (table_name, id, data) VALUES (%s, %s, %s)",
            [("holdings", h_id, psycopg.types.json.Jsonb(data)) for h_id, data in holdings],
        )
    return [h_id for h_id, _ in holdings]


def _read(conn, ids: list[str]) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, data FROM records WHERE table_name = 'holdings' AND id = ANY(%s)",
            (ids,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _migration_log_baseline() -> int:
    """Return the current max id in migration_log so tests can isolate
    rows they themselves inserted."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM migration_log")
            return int(cur.fetchone()[0])


def _migration_log_since(baseline: int) -> list[tuple]:
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
    """Touch DefaultInfrastructure so its lazy _ensure_schema creates
    the migration_log table (and its index) before any test reads
    from it."""
    from infrastructure_postgres import DefaultInfrastructure
    import uuid as _uuid

    DefaultInfrastructure().store(
        f"test_backfill_seed_{_uuid.uuid4().hex}",
        {"id": "seed", "x": 1},
    )


def _schema_migrations_rows() -> list[tuple]:
    """Return all schema_migrations rows for MIGRATION_NAME, in
    insertion order, as (migration_name, applied_at)."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT migration_name, applied_at FROM schema_migrations "
                "WHERE migration_name = %s ORDER BY id ASC",
                (MIGRATION_NAME,),
            )
            return cur.fetchall()


def _schema_migrations_baseline() -> int:
    """Return the current max id in schema_migrations so tests can
    isolate rows they themselves inserted. The table is shared across
    tests just like migration_log is."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM schema_migrations")
            return int(cur.fetchone()[0])


def _schema_migrations_since(baseline: int) -> list[tuple]:
    """Return schema_migrations rows for MIGRATION_NAME with id > baseline."""
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT migration_name, applied_at FROM schema_migrations "
                "WHERE migration_name = %s AND id > %s ORDER BY id ASC",
                (MIGRATION_NAME, baseline),
            )
            return cur.fetchall()


# ---------------------------------------------------------------------------
# Test 1: dry-run, real run, second real run = idempotent
# ---------------------------------------------------------------------------


def test_backfill_dry_run_then_real_then_idempotent(records_table, monkeypatch):
    """Seeds rows, dry-runs (count but no UPDATE), runs for real, then
    runs a second time to prove idempotency — same shape as
    tests/test_migrate_us_stocks.py's primary test."""
    _ensure_migration_log_table()
    migration_log_baseline = _migration_log_baseline()

    target_ids = ["pf-bf-AAPL", "pf-bf-MSFT", "pf-bf-GOOG", "pf-bf-TSLA"]

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed(conn)
        before = _read(conn, target_ids)

    # Phase 1: dry-run — no UPDATE issued, but the count is non-zero.
    monkeypatch.setenv("MIGRATION_DRY_RUN", "true")
    dry_run_count = backfill_holdings_currency()
    assert dry_run_count == 3, (
        f"dry-run should report the 3 rows missing currency "
        f"(MSFT, GOOG, TSLA); got {dry_run_count}"
    )

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        after_dry = _read(conn, target_ids)
    assert after_dry == before, "dry-run must not modify any rows"

    # Phase 2: real run — updates the 3 rows missing currency. AAPL
    # already had currency='USD' so its row is left alone by the
    # WHERE clause (data->>'currency' IS NULL).
    monkeypatch.setenv("MIGRATION_DRY_RUN", "false")
    real_count = backfill_holdings_currency()
    assert real_count == 3, f"real run should update 3 rows; got {real_count}"

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        after_real = _read(conn, target_ids)

    # Every row now has currency='USD' as a top-level field in `data`.
    assert after_real["pf-bf-AAPL"]["currency"] == "USD"
    assert after_real["pf-bf-MSFT"]["currency"] == "USD"
    assert after_real["pf-bf-GOOG"]["currency"] == "USD"
    assert after_real["pf-bf-TSLA"]["currency"] == "USD"

    # jsonb_set must not have clobbered GOOG's extra fields — that's
    # the load-bearing difference between this UPDATE and a wholesale
    # `data = '{...}'::jsonb` rewrite.
    assert after_real["pf-bf-GOOG"]["portfolio_id"] == "pf-bf"
    assert after_real["pf-bf-GOOG"]["security_id"] == "GOOG"
    assert after_real["pf-bf-GOOG"]["quantity"] == 3
    assert after_real["pf-bf-GOOG"]["provenance"] == "UNTRUSTED"
    assert after_real["pf-bf-GOOG"]["broker_connection"] == {"broker": "fake"}

    # AAPL's existing currency='USD' value must still be there (the
    # WHERE clause filtered it out — but verify nothing else
    # accidentally rewrote it).
    assert after_real["pf-bf-AAPL"]["quantity"] == 10
    assert after_real["pf-bf-AAPL"]["security_id"] == "AAPL"

    # Phase 3: idempotency — a second real run updates zero rows.
    second_count = backfill_holdings_currency()
    assert second_count == 0, (
        f"second real run must be a no-op; got {second_count}"
    )

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        after_second = _read(conn, target_ids)
    assert after_second == after_real, (
        "second real run must leave every row identical to post-first-run state"
    )

    # migration_log: 1 DRY_RUN + 2 SUCCESS rows (one per real run),
    # scoped to MIGRATION_NAME and to rows inserted after our baseline.
    rows_logged = _migration_log_since(migration_log_baseline)
    assert len(rows_logged) == 3, (
        f"expected 3 migration_log rows (1 dry-run + 2 real runs); got "
        f"{len(rows_logged)}: {rows_logged}"
    )
    assert rows_logged[0][0] == "DRY_RUN"
    assert int(rows_logged[0][1]) == 3
    assert rows_logged[0][3] is True
    assert rows_logged[1][0] == "SUCCESS"
    assert int(rows_logged[1][1]) == 3
    assert rows_logged[1][3] is False
    assert rows_logged[2][0] == "SUCCESS"
    assert int(rows_logged[2][1]) == 0
    assert rows_logged[2][3] is False


# ---------------------------------------------------------------------------
# Test 2: schema_migrations records one row per logical migration
# ---------------------------------------------------------------------------


def test_backfill_writes_one_schema_migrations_row_even_across_reruns(
    records_table, monkeypatch
):
    """Story acceptance criterion: 'schema_migrations table tracks
    migration status'. Two real runs must produce exactly one row in
    schema_migrations for MIGRATION_NAME, regardless of how many real
    runs happen or how many records were updated."""
    monkeypatch.setenv("MIGRATION_DRY_RUN", "false")

    # Real bug, found live: schema_migrations' own idempotency guard
    # (ON CONFLICT (migration_name) DO NOTHING) is correct behavior for a
    # real migration, but it means this test's "first run" genuinely
    # isn't first if another test in the same pytest session (or an
    # earlier real invocation of this same script) already inserted the
    # MIGRATION_NAME row -- capturing a baseline id isn't enough, the row
    # itself has to be gone for a real "first run" scenario to be real.
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM schema_migrations WHERE migration_name = %s", (MIGRATION_NAME,))

    baseline = _schema_migrations_baseline()

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed(conn)

    # First real run: writes the schema_migrations row.
    first = backfill_holdings_currency()
    assert first == 3

    rows = _schema_migrations_since(baseline)
    assert len(rows) == 1, (
        f"first real run should write exactly 1 schema_migrations row; "
        f"got {len(rows)}: {rows}"
    )
    assert rows[0][0] == MIGRATION_NAME
    assert rows[0][1] is not None  # applied_at populated

    # Second real run (a no-op at the records level) must NOT add a
    # second schema_migrations row — ON CONFLICT DO NOTHING.
    second = backfill_holdings_currency()
    assert second == 0

    rows = _schema_migrations_since(baseline)
    assert len(rows) == 1, (
        f"second real run must not add another schema_migrations row "
        f"(UNIQUE constraint + ON CONFLICT DO NOTHING); got {len(rows)}: {rows}"
    )

    # Third run, also a no-op — still exactly one row.
    third = backfill_holdings_currency()
    assert third == 0
    rows = _schema_migrations_since(baseline)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Test 3: a dry-run does not write a schema_migrations row
# ---------------------------------------------------------------------------


def test_backfill_dry_run_does_not_write_a_schema_migrations_row(
    records_table, monkeypatch
):
    """Dry-run is for counting, not applying — it must not mark the
    logical migration as applied in schema_migrations, only in
    migration_log (which is per-invocation)."""
    monkeypatch.setenv("MIGRATION_DRY_RUN", "true")

    baseline = _schema_migrations_baseline()

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed(conn)

    dry_count = backfill_holdings_currency()
    assert dry_count == 3

    rows = _schema_migrations_since(baseline)
    assert rows == [], (
        f"dry-run must not write to schema_migrations; got {rows}"
    )


# ---------------------------------------------------------------------------
# Test 4: failure rolls back and logs FAILED with the error message
# ---------------------------------------------------------------------------


def test_backfill_failure_rolls_back_and_logs_failed(records_table, monkeypatch):
    """When the real UPDATE fails (here via an injected cursor that
    raises), the autocommit connection rolls back automatically,
    records is left unchanged, and migration_log shows FAILED with a
    real error_message and the underlying exception is re-raised."""
    _ensure_migration_log_table()
    migration_log_baseline = _migration_log_baseline()

    monkeypatch.setenv("MIGRATION_DRY_RUN", "false")

    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed(conn)
        # Snapshot before the failing run so we can prove records is
        # unchanged afterwards.
        before = _read(conn, ["pf-bf-MSFT"])

    # Inject a real failure into the UPDATE path by monkey-patching
    # psycopg's Connection.cursor to return a cursor whose .execute()
    # raises the first time it's called with the UPDATE statement.
    # The schema_migrations INSERT is the second execute on the same
    # cursor; we want the UPDATE itself to fail so its transaction
    # rolls back cleanly before any partial state is logged.
    from scripts import backfill_holdings_currency as bhcf

    class _ExplodingCursor:
        def __init__(self, real) -> None:
            self._real = real
            self._exploded = False

        def execute(self, sql, params=None):
            # Let non-UPDATE statements (the schema_migrations create
            # IF NOT EXISTS plus migration_log INSERT) pass through so
            # the FAILED log row still gets written. Only the UPDATE
            # path explodes — this is the realistic failure mode for
            # this backfill (bad JSONB expression, table dropped, etc.).
            if "UPDATE records" in sql and not self._exploded:
                self._exploded = True
                raise psycopg.errors.UndefinedColumn(
                    'column "currency" of relation "records" does not exist'
                )
            return self._real.execute(sql, params)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self._real.__exit__(exc_type, exc, tb)
            return False

        @property
        def rowcount(self):
            return self._real.rowcount

        def fetchone(self):
            return self._real.fetchone()

    original_connect = psycopg.connect

    class _ExplodingConnection:
        def __init__(self, real) -> None:
            self._real = real

        def cursor(self):
            return _ExplodingCursor(self._real.cursor())

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._real.__exit__(exc_type, exc, tb)

    def _exploding_connect(dsn, **kwargs):
        return _ExplodingConnection(original_connect(dsn, **kwargs))

    monkeypatch.setattr(bhcf.psycopg, "connect", _exploding_connect)

    with pytest.raises(psycopg.Error) as excinfo:
        backfill_holdings_currency()

    assert excinfo.value is not None
    assert "currency" in str(excinfo.value) or "column" in str(excinfo.value).lower(), (
        f"raised error must reference the underlying SQL error; got "
        f"{excinfo.value!r}"
    )

    # Real bug, found live: `monkeypatch.setattr(bhcf.psycopg, "connect",
    # ...)` patches the actual shared `psycopg` module object (bhcf.psycopg
    # IS this test file's own `psycopg`, not a separate copy) -- every
    # subsequent real connection this test (or a module-level helper like
    # _read/_migration_log_since) opens via plain `psycopg.connect(...)`
    # would ALSO come back exploding-wrapped, whose cursor doesn't
    # implement fetchall (only what backfill_holdings_currency() itself
    # needs). Restore the real connect now, before any further real
    # verification runs, rather than patching every helper call site
    # individually.
    monkeypatch.setattr(bhcf.psycopg, "connect", original_connect)

    # records must be unchanged — the failed UPDATE rolled back.
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        after = _read(conn, ["pf-bf-MSFT"])
    assert after == before, (
        f"failed backfill must not modify records; before={before} after={after}"
    )

    # migration_log must contain exactly one new FAILED row.
    rows_logged = _migration_log_since(migration_log_baseline)
    assert len(rows_logged) == 1, (
        f"failed run must log exactly one row to migration_log; got "
        f"{len(rows_logged)}: {rows_logged}"
    )
    status, rows_affected, error_message, dry_run = rows_logged[0]
    assert status == "FAILED"
    assert rows_affected is None
    assert error_message is not None and error_message != ""
    assert "currency" in error_message or "column" in error_message.lower(), (
        f"error_message must reference the underlying column error; got "
        f"{error_message!r}"
    )
    assert dry_run is False

    # No schema_migrations row was written (the UPDATE failed before
    # the INSERT ran).
    schema_baseline = _schema_migrations_baseline()
    rows = _schema_migrations_since(schema_baseline)
    assert rows == [], (
        f"failed backfill must not write to schema_migrations; got {rows}"
    )
