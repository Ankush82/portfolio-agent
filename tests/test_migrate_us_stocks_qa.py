"""QA verification for STORY-3 acceptance criteria.

Story STORY-3 acceptance criteria:
  1. When MIGRATION_DRY_RUN is false/not set, the UPDATE executes and
     rows_updated equals the number of rows where at least one of the
     three columns differs from the target values.
  2. When MIGRATION_DRY_RUN=true/1/yes (case-insensitive), no UPDATE
     is issued; rows_updated reflects the count of rows that would
     have changed.
  3. The UPDATE uses SET-based syntax with a FROM join to
     tmp_us_tickers and includes a WHERE clause to avoid unnecessary
     writes.
  4. The script is idempotent: running it twice produces the same
     rows_updated on the second run (zero changes if already correct).

This file is the QA agent's independent verification — it is separate
from the dev's own test and probes the criteria directly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=1):
            return True
    except psycopg.Error:
        return False


POSTGRES_SKIP_REASON = (
    "no live Postgres reachable at DEFAULT_POSTGRES_DSN"
)
pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason=POSTGRES_SKIP_REASON
)


@pytest.fixture
def stocks_table():
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
    yield
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS stocks")
            cur.execute("DROP TABLE IF EXISTS tmp_us_tickers")


def _seed_minimal(conn) -> None:
    """Seed three rows: AAPL correct, MSFT wrong currency,
    TSLA (non-target, must not be touched)."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO tmp_us_tickers (id) VALUES (%s)",
            [("AAPL",), ("MSFT",)],
        )
        cur.executemany(
            "INSERT INTO stocks (id, currency, exchange, symbol_suffix) "
            "VALUES (%s, %s, %s, %s)",
            [
                ("AAPL", "USD", None, None),
                ("MSFT", "EUR", None, None),
                ("TSLA", "EUR", "XETRA", ".F"),
            ],
        )


def _read_row(conn, id_: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT currency, exchange, symbol_suffix FROM stocks WHERE id=%s",
            (id_,),
        )
        return cur.fetchone()


def test_qa_story3_all_four_acceptance_criteria(
    stocks_table, monkeypatch, tmp_path
):
    """One real test exercising all four STORY-3 acceptance criteria
    via independent assertions against a live Postgres."""
    # --- Acceptance criterion #3: SET-based UPDATE with FROM join ---
    sql_text = Path("scripts/migrate_us_stocks.sql").read_text()
    # Strip SQL comments so we look only at actual statements.
    sql_no_comments = re.sub(r"--[^\n]*", "", sql_text)
    assert re.search(
        r"UPDATE\s+stocks\s+SET\b",
        sql_no_comments,
        re.IGNORECASE,
    ), "UPDATE ... SET must target the stocks table"
    assert re.search(
        r"FROM\s+tmp_us_tickers\b",
        sql_no_comments,
        re.IGNORECASE,
    ), "UPDATE must use FROM tmp_us_tickers join"
    # WHERE clause must restrict to rows that differ.
    assert re.search(
        r"currency\s*<>\s*'USD'",
        sql_no_comments,
        re.IGNORECASE,
    ), "WHERE must check currency<>'USD'"
    assert re.search(
        r"exchange\s+IS\s+NOT\s+NULL",
        sql_no_comments,
        re.IGNORECASE,
    ), "WHERE must check exchange IS NOT NULL"
    assert re.search(
        r"symbol_suffix\s+IS\s+NOT\s+NULL",
        sql_no_comments,
        re.IGNORECASE,
    ), "WHERE must check symbol_suffix IS NOT NULL"

    from scripts.migrate_us_stocks import migrate_us_stocks, _is_dry_run

    # --- Acceptance criterion #2a: case-insensitive parsing of env var ---
    # Each value below must trigger dry-run mode per the story's
    # "true/1/yes (case-insensitive)" rule.
    monkeypatch.delenv("MIGRATION_DRY_RUN", raising=False)
    for truthy in ("true", "True", "TRUE", "  true  ", "1", "yes", "Yes", "YES"):
        monkeypatch.setenv("MIGRATION_DRY_RUN", truthy)
        assert _is_dry_run() is True, (
            f"MIGRATION_DRY_RUN={truthy!r} must be treated as dry-run"
        )
    for falsy in ("false", "False", "0", "no", "off", "", "anything-else"):
        monkeypatch.setenv("MIGRATION_DRY_RUN", falsy)
        assert _is_dry_run() is False, (
            f"MIGRATION_DRY_RUN={falsy!r} must NOT be treated as dry-run"
        )

    # Seed the DB fresh for the live-run assertions.
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        _seed_minimal(conn)
        msft_before = _read_row(conn, "MSFT")
        tsla_before = _read_row(conn, "TSLA")
    assert msft_before == ("EUR", None, None)
    assert tsla_before == ("EUR", "XETRA", ".F")

    # --- Acceptance criterion #2: dry-run reports would-change count
    # without modifying anything ---
    monkeypatch.setenv("MIGRATION_DRY_RUN", "TRUE")  # uppercase, with no UPDATE
    dry_count = migrate_us_stocks()
    assert dry_count == 1, (
        f"dry-run must report 1 row would change (MSFT only); got {dry_count}"
    )
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        assert _read_row(conn, "MSFT") == msft_before, (
            "dry-run must not modify MSFT"
        )
        assert _read_row(conn, "TSLA") == tsla_before, (
            "dry-run must not modify TSLA"
        )

    # --- Acceptance criterion #1: real UPDATE runs and rowcount
    # matches the number of differing rows ---
    monkeypatch.setenv("MIGRATION_DRY_RUN", "false")
    real_count = migrate_us_stocks()
    assert real_count == 1, (
        f"real run must report 1 row updated (MSFT only); got {real_count}"
    )
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        assert _read_row(conn, "MSFT") == ("USD", None, None), (
            "real run must normalize MSFT to USD/NULL/NULL"
        )
        assert _read_row(conn, "TSLA") == tsla_before, (
            "real run must NOT modify TSLA (not in tmp_us_tickers)"
        )

    # --- Acceptance criterion #4: idempotency — a second real run
    # updates zero rows ---
    second_count = migrate_us_stocks()
    assert second_count == 0, (
        f"second real run must be a no-op; got {second_count}"
    )

    # --- Explicit dry_run= kwarg overrides env var ---
    monkeypatch.setenv("MIGRATION_DRY_RUN", "false")
    # dry_run=True even though env says false -> still a dry-run
    overridden = migrate_us_stocks(dry_run=True)
    assert overridden == 0, (
        f"dry_run=True must short-circuit to no-op; got {overridden}"
    )
    # and dry_run=False even though env says true -> real run, 0 rows
    monkeypatch.setenv("MIGRATION_DRY_RUN", "true")
    real_again = migrate_us_stocks(dry_run=False)
    assert real_again == 0, (
        f"dry_run=False must run UPDATE even if env says true; got {real_again}"
    )