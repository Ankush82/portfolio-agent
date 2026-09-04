"""Tests for scripts/detect_delisted_stocks.py (Story STORY-13).

Acceptance-criteria coverage:
  AC3 - "Delisted/suspended status is stored in database for each stock"
        (real Postgres via the existing DefaultInfrastructure /
        records JSONB store, with a fresh uuid-namespaced table per
        test to keep tests hermetic across reruns)
  AC4 - "System detects both NSE/BSE and US stock delistings" (NSE,
        BSE and bare US tickers all flow through the same run with
        no exchange-group special-casing)
  AC5 - "If API does not provide delisting status, check is skipped
        without error" (a quote dict without a 'tradeable' key
        results in a `skipped_no_tradeable` count, no delisting_status
        row, no migration_log change-event row, and the batch
        continues)
  AC6 - "Batch job logs all status changes" (a fresh symbol gets an
        INITIAL_RECORD migration_log row tagged with the distinct
        DELISTING_CHECK_NAME; a status transition gets a
        STATUS_CHANGE row; a no-change check logs nothing)
  AC7 - "System handles API failures during batch job gracefully"
        (YahooFinanceError skips one symbol and the batch keeps
        running for the rest)

AC1 (daily 1 AM UTC cron) and AC2 (checks Yahoo Finance API
'tradeable' flag) are not unit-testable here — the cron expression
lives in .github/workflows/delisting_check.yml and the Yahoo Finance
parsing lives in src/yahoo_finance_client.py. Those are covered by
the workflow file itself and `tests/test_yahoo_finance_client.py`
respectively.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

from infrastructure_postgres import DEFAULT_POSTGRES_DSN, DefaultInfrastructure

import api_error_logging
import yahoo_finance_client
import detect_delisted_stocks as detect_delisted_stocks_module
from detect_delisted_stocks import (
    DELISTING_CHECK_NAME,
    DELISTING_DRY_RUN,
    detect_delisted_stocks,
)
from yahoo_finance_client import YahooFinanceError


def _postgres_available() -> bool:
    try:
        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=1):
            return True
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _postgres_available(),
    reason=(
        "no live Postgres reachable at DEFAULT_POSTGRES_DSN — "
        "run `docker-compose up -d` for real coverage"
    ),
)


# --- hermetic infra fixtures ---------------------------------------------


@pytest.fixture
def holdings_table() -> str:
    """Per-test holdings table name so tests don't collide on
    `records.table_name='holdings'`."""
    return f"holdings_test_{uuid.uuid4().hex}"


@pytest.fixture
def infra():
    """Real `DefaultInfrastructure` against the project-default
    Postgres DSN. The first method call triggers `_ensure_schema`,
    creating `records` and `migration_log` if they aren't already
    there."""
    return DefaultInfrastructure()


# `delisting_status` rows are keyed deterministically by symbol (e.g.
# "AAPL", "NSE:RELIANCE.NS") rather than a per-test uuid like
# `holdings_table` -- several tests below reuse the same handful of
# fixed symbols, so a row written by an earlier test in this same real
# Postgres session would otherwise leak into a later test that expects
# a fresh first-check. Clear known ids before every test, mirroring
# the DELETE-before-first-run pattern already used in
# test_backfill_holdings_currency.py / test_verify_holdings_currency_backfill.py.
_TEST_DELISTING_STATUS_IDS = [
    "AAPL", "MSFT", "BADTICKER", "NSE:RELIANCE.NS", "BSE:TCS.BO",
]


@pytest.fixture(autouse=True)
def _clean_delisting_status_between_tests():
    if not _postgres_available():
        yield
        return
    with psycopg.connect(DEFAULT_POSTGRES_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM records WHERE table_name = 'delisting_status' AND id = ANY(%s)",
                (_TEST_DELISTING_STATUS_IDS,),
            )
            cur.execute(
                "DELETE FROM migration_log WHERE migration_name = %s",
                (DELISTING_CHECK_NAME,),
            )
    yield


def _seed_holdings(infra: DefaultInfrastructure, table: str, security_ids: list[str]) -> None:
    """Seed distinct `holdings` rows (one per symbol) into the
    per-test table — `detect_delisted_stocks` derives distinct
    symbols from `data->>'security_id'`, which is what the real
    `holdings` rows look like in production."""
    for idx, symbol in enumerate(security_ids):
        infra.store(
            table,
            {
                "id": f"h-{idx}-{uuid.uuid4().hex}",
                "portfolio_id": "p1",
                "security_id": symbol,
                "quantity": 1,
            },
        )


def _seed_holdings_with_duplicates(infra: DefaultInfrastructure, table: str, security_ids: list[str]) -> None:
    """Seed `holdings` rows where the same `security_id` appears more
    than once across different holding ids — the distinct-symbol
    query must collapse these to a single fetcher call per symbol."""
    for symbol in security_ids:
        for _ in range(2):
            infra.store(
                table,
                {
                    "id": f"h-{uuid.uuid4().hex}",
                    "portfolio_id": "p1",
                    "security_id": symbol,
                    "quantity": 1,
                },
            )


def _read_migration_log(infra: DefaultInfrastructure, migration_name: str) -> list[dict]:
    """Read all `migration_log` rows tagged with `migration_name`,
    ordered by id (insertion order). Returns the raw JSONB `data`
    column — actually `migration_log` is fixed-schema so we read
    the columns directly."""
    with infra._connection().cursor() as cursor:
        cursor.execute(
            """
            SELECT migration_name, status, rows_affected, error_message, dry_run
            FROM migration_log
            WHERE migration_name = %s
            ORDER BY id
            """,
            (migration_name,),
        )
        return [
            {
                "migration_name": row[0],
                "status": row[1],
                "rows_affected": row[2],
                "error_message": row[3],
                "dry_run": row[4],
            }
            for row in cursor.fetchall()
        ]


def _delisting_status_for(infra: DefaultInfrastructure, record_id: str) -> dict | None:
    return infra.retrieve("delisting_status", record_id)


# --- AC4: NSE + BSE + US in a single run, no exchange-group special-case -


@requires_postgres
def test_detects_nse_bse_and_us_symbols_in_one_run(infra, monkeypatch, holdings_table):
    """AC4: every distinct `security_id` across holdings is checked
    in a single run. NSE, BSE and bare-US tickers all flow through
    the same fetcher with no exchange-group branching. We feed
    quotes with `tradeable=True` so no change-events are emitted
    on the first run; what we verify is that the fetcher was
    called once per distinct symbol (i.e. NSE+US truly share one
    batch path)."""
    _seed_holdings(
        infra,
        holdings_table,
        ["RELIANCE.NS", "TCS.BO", "AAPL", "MSFT"],
    )

    calls: list[str] = []

    def fake_fetcher(symbol: str) -> dict:
        calls.append(symbol)
        return {
            "symbol": symbol,
            "tradeable": True,
            "current_price": 1.0,
        }

    monkeypatch.setattr(detect_delisted_stocks_module, "fetch_yahoo_finance_quote", fake_fetcher)

    # Override `detect_delisted_stocks`'s holdings table name by
    # pointing the helper at our per-test table. The helper
    # hard-codes 'holdings' for clarity, but we want a hermetic
    # test — substitute via monkeypatch.
    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)

    summary = detect_delisted_stocks(infra, dry_run=False)

    # Every distinct symbol from the holdings table got exactly one
    # fetcher call. No special-casing: NSE/BSE (suffix) and US (bare)
    # tickers are all in there.
    assert sorted(calls) == ["AAPL", "MSFT", "RELIANCE.NS", "TCS.BO"]

    # First run for every symbol = INITIAL_RECORD change-events.
    # All four fired, none skipped.
    assert summary["symbols_checked"] == 4
    assert summary["skipped_api_error"] == 0
    assert summary["skipped_no_tradeable"] == 0
    assert summary["changes_logged"] == 4


@requires_postgres
def test_collapses_duplicate_holdings_to_distinct_symbols(infra, monkeypatch, holdings_table):
    """If two holdings rows carry the same `security_id`, the
    script still only calls the fetcher once per distinct symbol —
    no duplicate API calls."""
    _seed_holdings_with_duplicates(infra, holdings_table, ["RELIANCE.NS", "AAPL"])

    calls: list[str] = []

    def fake_fetcher(symbol: str) -> dict:
        calls.append(symbol)
        return {"symbol": symbol, "tradeable": True}

    monkeypatch.setattr(detect_delisted_stocks_module, "fetch_yahoo_finance_quote", fake_fetcher)
    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)

    summary = detect_delisted_stocks(infra, dry_run=False)

    assert sorted(calls) == ["AAPL", "RELIANCE.NS"]
    assert summary["symbols_checked"] == 2
    assert summary["changes_logged"] == 2  # first-run INITIAL_RECORD per symbol


# --- AC5: 'tradeable' absent → skip without error -----------------------


@requires_postgres
def test_skips_symbol_when_tradeable_key_absent(infra, monkeypatch, holdings_table):
    """AC5: a quote dict that does not carry 'tradeable' (because
    Yahoo's chart endpoint legitimately doesn't always surface
    `meta.tradeable`) is skipped silently — no exception raised, no
    delisting_status row, no change-event log row. The batch
    continues for the other symbols."""
    _seed_holdings(infra, holdings_table, ["RELIANCE.NS", "AAPL", "TCS.BO"])

    def fake_fetcher(symbol: str) -> dict:
        # RELIANCE.NS gets a real tradeable flag; AAPL and TCS.BO
        # both come back without one.
        if symbol == "RELIANCE.NS":
            return {"symbol": symbol, "tradeable": True}
        return {"symbol": symbol, "current_price": 1.0}  # no 'tradeable'

    monkeypatch.setattr(detect_delisted_stocks_module, "fetch_yahoo_finance_quote", fake_fetcher)
    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)

    summary = detect_delisted_stocks(infra, dry_run=False)

    assert summary["skipped_no_tradeable"] == 2
    assert summary["symbols_checked"] == 1
    assert summary["skipped_api_error"] == 0

    # Only RELIANCE.NS got a delisting_status row; AAPL and TCS.BO
    # didn't (because their quotes had no 'tradeable' field).
    assert _delisting_status_for(infra, "NSE:RELIANCE.NS") is not None
    assert _delisting_status_for(infra, "AAPL") is None
    assert _delisting_status_for(infra, "BSE:TCS.BO") is None

    # migration_log: one INITIAL_RECORD per distinct *change*. We
    # had two no-tradeable skips (no change-event), one
    # INITIAL_RECORD for RELIANCE.NS (change), and one invocation
    # breadcrumb (status=SUCCESS).
    log_rows = _read_migration_log(infra, DELISTING_CHECK_NAME)
    change_events = [r for r in log_rows if r["status"] in ("INITIAL_RECORD", "STATUS_CHANGE")]
    invocation_rows = [r for r in log_rows if r["status"] == "SUCCESS"]
    assert len(change_events) == 1
    assert change_events[0]["status"] == "INITIAL_RECORD"
    assert "RELIANCE.NS" in change_events[0]["error_message"]
    assert len(invocation_rows) == 1


# --- AC7: API failures skip one symbol, batch keeps running -------------


@requires_postgres
def test_yahoo_finance_error_skips_one_symbol_and_continues(infra, monkeypatch, holdings_table):
    """AC7: a `YahooFinanceError` on one symbol is logged + skipped;
    the run continues for the rest. Three symbols, one raises,
    two succeed — summary.skipped_api_error==1 and
    summary.symbols_checked==2."""
    _seed_holdings(infra, holdings_table, ["AAPL", "BADTICKER", "MSFT"])

    def fake_fetcher(symbol: str) -> dict:
        if symbol == "BADTICKER":
            raise YahooFinanceError(
                f"Yahoo Finance does not recognise symbol 'BADTICKER' on (unknown) "
                f"(upstream returned a non-2xx response). Please verify the symbol and try again."
            )
        return {"symbol": symbol, "tradeable": True}

    monkeypatch.setattr(detect_delisted_stocks_module, "fetch_yahoo_finance_quote", fake_fetcher)
    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)

    summary = detect_delisted_stocks(infra, dry_run=False)

    assert summary["skipped_api_error"] == 1
    assert summary["symbols_checked"] == 2
    assert summary["skipped_no_tradeable"] == 0

    # The two good symbols got rows; BADTICKER didn't.
    assert _delisting_status_for(infra, "AAPL") is not None
    assert _delisting_status_for(infra, "MSFT") is not None
    assert _delisting_status_for(infra, "BADTICKER") is None

    # Two INITIAL_RECORD change-events (one for AAPL, one for MSFT)
    # — never for BADTICKER. Plus one invocation breadcrumb.
    log_rows = _read_migration_log(infra, DELISTING_CHECK_NAME)
    change_events = [r for r in log_rows if r["status"] == "INITIAL_RECORD"]
    assert len(change_events) == 2
    for event in change_events:
        # The symbol must be one of the two good ones — never BADTICKER.
        assert "BADTICKER" not in event["error_message"]


@requires_postgres
def test_unexpected_fetcher_exception_is_also_contained(infra, monkeypatch, holdings_table):
    """Defence in depth: a non-YahooFinanceError from the fetcher
    (e.g. a programming bug) is also caught — the batch keeps
    going. The summary still reflects the skip."""
    _seed_holdings(infra, holdings_table, ["AAPL", "BROKEN"])

    def fake_fetcher(symbol: str) -> dict:
        if symbol == "BROKEN":
            raise RuntimeError("totally unexpected")
        return {"symbol": symbol, "tradeable": True}

    monkeypatch.setattr(detect_delisted_stocks_module, "fetch_yahoo_finance_quote", fake_fetcher)
    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)

    summary = detect_delisted_stocks(infra, dry_run=False)

    assert summary["skipped_api_error"] == 1
    assert summary["symbols_checked"] == 1


# --- AC6: log every status CHANGE, not every check ----------------------


@requires_postgres
def test_no_change_logs_no_event(infra, monkeypatch, holdings_table):
    """AC6: "log every status CHANGE (not every check)". A second
    run with the same tradeable=True as the first run does NOT
    emit a new change-event row — the only new migration_log row
    is the per-run invocation breadcrumb."""
    _seed_holdings(infra, holdings_table, ["AAPL"])

    def fake_fetcher(symbol: str) -> dict:
        return {"symbol": symbol, "tradeable": True}

    monkeypatch.setattr(detect_delisted_stocks_module, "fetch_yahoo_finance_quote", fake_fetcher)
    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)

    # First run: AAPL is a fresh symbol → INITIAL_RECORD change-event.
    first_summary = detect_delisted_stocks(infra, dry_run=False)
    assert first_summary["changes_logged"] == 1

    log_after_first = _read_migration_log(infra, DELISTING_CHECK_NAME)
    assert len([r for r in log_after_first if r["status"] == "INITIAL_RECORD"]) == 1

    # Second run: same tradeable=True → no change-event.
    second_summary = detect_delisted_stocks(infra, dry_run=False)
    assert second_summary["symbols_checked"] == 1
    assert second_summary["changes_logged"] == 0  # no change

    log_after_second = _read_migration_log(infra, DELISTING_CHECK_NAME)
    # Still exactly one INITIAL_RECORD from the first run, plus one
    # extra invocation breadcrumb (SUCCESS) for the second run.
    assert len([r for r in log_after_second if r["status"] == "INITIAL_RECORD"]) == 1
    assert len([r for r in log_after_second if r["status"] == "SUCCESS"]) == 2


@requires_postgres
def test_transition_from_tradeable_to_delisted_logs_change(infra, monkeypatch, holdings_table):
    """AC6: a status transition (True → False) DOES emit a
    STATUS_CHANGE event."""
    _seed_holdings(infra, holdings_table, ["AAPL"])

    state = {"tradeable": True}

    def fake_fetcher(symbol: str) -> dict:
        return {"symbol": symbol, "tradeable": state["tradeable"]}

    monkeypatch.setattr(detect_delisted_stocks_module, "fetch_yahoo_finance_quote", fake_fetcher)
    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)

    # First run: tradeable=True → INITIAL_RECORD with is_delisted=False.
    detect_delisted_stocks(infra, dry_run=False)
    first_row = _delisting_status_for(infra, "AAPL")
    assert first_row is not None and first_row["is_delisted"] is False

    # Second run: tradeable flips to False → STATUS_CHANGE.
    state["tradeable"] = False
    summary = detect_delisted_stocks(infra, dry_run=False)
    assert summary["changes_logged"] == 1

    second_row = _delisting_status_for(infra, "AAPL")
    assert second_row is not None and second_row["is_delisted"] is True
    assert second_row["checked_at"] >= first_row["checked_at"]

    log_rows = _read_migration_log(infra, DELISTING_CHECK_NAME)
    transitions = [r for r in log_rows if r["status"] == "STATUS_CHANGE"]
    assert len(transitions) == 1
    assert "False -> True" in transitions[0]["error_message"] or "is_delisted False -> True" in transitions[0]["error_message"]


@requires_postgres
def test_change_event_log_uses_distinct_migration_name(infra, monkeypatch, holdings_table):
    """The "distinct log entry type" requirement: delisting change-
    events live in `migration_log` but are queryable separately
    from schema migrations by filtering on `migration_name`."""
    _seed_holdings(infra, holdings_table, ["AAPL"])

    def fake_fetcher(symbol: str) -> dict:
        return {"symbol": symbol, "tradeable": True}

    monkeypatch.setattr(detect_delisted_stocks_module, "fetch_yahoo_finance_quote", fake_fetcher)
    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)

    detect_delisted_stocks(infra, dry_run=False)

    # A second story's migration (say, a schema migration) sits in
    # the same migration_log table with a different migration_name.
    # Write a sentinel row and confirm the delisting-check filter
    # excludes it.
    with infra._connection().cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO migration_log
                (migration_name, run_at, status, rows_affected, error_message, dry_run)
            VALUES (%s, now(), %s, %s, %s, %s)
            """,
            ("some_other_migration_v999", "SUCCESS", 0, "unrelated", False),
        )

    delisting_only = _read_migration_log(infra, DELISTING_CHECK_NAME)
    assert delisting_only, "no migration_log rows found for DELISTING_CHECK_NAME"
    for row in delisting_only:
        assert row["migration_name"] == DELISTING_CHECK_NAME
        assert "some_other_migration_v999" not in (row["error_message"] or "")


# --- AC3: delisting_status rows are stored for each stock ----------------


@requires_postgres
def test_delisting_status_row_shape_matches_spec(infra, monkeypatch, holdings_table):
    """AC3: every checked symbol gets a `delisting_status` row in
    the database — exactly the fields the story mandates:
    `symbol`, `exchange`, `is_delisted`, `checked_at`, `source`.
    `is_delisted` is the inverse of `tradeable` (True ⇔ not
    tradeable)."""
    _seed_holdings(infra, holdings_table, ["RELIANCE.NS", "AAPL"])

    state = {"RELIANCE.NS": True, "AAPL": False}

    def fake_fetcher(symbol: str) -> dict:
        return {"symbol": symbol, "tradeable": state[symbol]}

    monkeypatch.setattr(detect_delisted_stocks_module, "fetch_yahoo_finance_quote", fake_fetcher)
    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)

    detect_delisted_stocks(infra, dry_run=False)

    nse_row = _delisting_status_for(infra, "NSE:RELIANCE.NS")
    us_row = _delisting_status_for(infra, "AAPL")

    for row in (nse_row, us_row):
        assert row is not None
        assert set(row.keys()) >= {"symbol", "exchange", "is_delisted", "checked_at", "source"}
        assert row["source"] == "yahoo_tradeable_flag"
        assert row["checked_at"]  # ISO timestamp string, non-empty

    assert nse_row["symbol"] == "RELIANCE.NS"
    assert nse_row["exchange"] == "NSE"
    assert nse_row["is_delisted"] is False  # tradeable=True ⇔ is_delisted=False

    assert us_row["symbol"] == "AAPL"
    assert us_row["exchange"] == ""  # bare US ticker, no .NS/.BO suffix
    assert us_row["is_delisted"] is True  # tradeable=False ⇔ is_delisted=True


# --- dry-run: writes migration_log change-events but no delisting_status -


@requires_postgres
def test_dry_run_does_not_write_delisting_status_rows(infra, monkeypatch, holdings_table):
    """Dry-run mode is the migration-style "predict what would
    change" path: it logs the change-event breadcrumb so a
    monitoring query can see what *would* happen, but it does not
    write a `delisting_status` row."""
    _seed_holdings(infra, holdings_table, ["AAPL"])

    def fake_fetcher(symbol: str) -> dict:
        return {"symbol": symbol, "tradeable": True}

    monkeypatch.setattr(detect_delisted_stocks_module, "fetch_yahoo_finance_quote", fake_fetcher)
    monkeypatch.setattr(detect_delisted_stocks_module, "_HOLDINGS_TABLE", holdings_table)

    summary = detect_delisted_stocks(infra, dry_run=True)

    assert summary["changes_logged"] == 1
    assert _delisting_status_for(infra, "AAPL") is None

    # Change-event was logged (with dry_run=True so a monitoring
    # query can tell dry-run change-events from real ones).
    log_rows = _read_migration_log(infra, DELISTING_CHECK_NAME)
    initial_records = [r for r in log_rows if r["status"] == "INITIAL_RECORD"]
    assert len(initial_records) == 1
    assert initial_records[0]["dry_run"] is True


# --- module-level contract checks ----------------------------------------


def test_delisting_check_name_is_a_distinct_constant():
    """The "distinct log entry type" requirement is satisfied by a
    dedicated constant on the module. This test pins that constant
    so a refactor can't silently merge delisting change-events
    back into the schema-migration noise."""
    assert DELISTING_CHECK_NAME == "delisting_status_change_v1"
    assert DELISTING_CHECK_NAME != "us_stock_portfolio_defaults_v1"  # migrate_us_stocks.py's name
    assert DELISTING_CHECK_NAME != "holdings_currency_backfill_v1"  # backfill_holdings_currency.py's name


def test_dry_run_env_var_is_case_insensitive_truthy(monkeypatch):
    """DELISTING_DRY_RUN follows the same case-insensitive true/1/
    yes gating as MIGRATION_DRY_RUN — but uses its own env-var
    name so the delisting check can be run dry without affecting
    any in-flight migrations."""
    for truthy in ("true", "True", "TRUE", "1", "yes", "YES"):
        monkeypatch.setenv("DELISTING_DRY_RUN", truthy)
        assert detect_delisted_stocks_module._is_dry_run() is True

    for falsy in ("false", "False", "0", "no", "", "anything-else"):
        monkeypatch.setenv("DELISTING_DRY_RUN", falsy)
        assert detect_delisted_stocks_module._is_dry_run() is False


def test_workflow_yaml_uses_one_am_utc_daily_cron():
    """AC1: the workflow file declares the daily 1 AM UTC cron
    schedule. We parse it as YAML so a typo in the cron expression
    fails this test immediately."""
    import yaml  # type: ignore[import-untyped]

    workflow_path = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "delisting_check.yml"
    )
    assert workflow_path.is_file(), f"workflow file missing: {workflow_path}"

    with workflow_path.open("r") as f:
        workflow = yaml.safe_load(f)

    # AC1: schedule includes "0 1 * * *".
    on = workflow.get(True) if isinstance(workflow.get(True), dict) else workflow.get("on", {})
    schedule = on.get("schedule", [])
    cron_expressions = [entry["cron"] for entry in schedule if "cron" in entry]
    assert "0 1 * * *" in cron_expressions, (
        f"expected daily 1 AM UTC cron ('0 1 * * *') in .github/workflows/delisting_check.yml, "
        f"got {cron_expressions}"
    )

    # Manual re-run also present (so on-call can rerun without
    # waiting for the next cron tick).
    assert "workflow_dispatch" in on

    # Distinct concurrency group from the migration workflow.
    assert workflow["concurrency"]["group"] == "delisting-check"


def test_workflow_yaml_uses_postgres_service_container():
    """AC2 + AC7: the workflow provisions a real Postgres service
    container (matching `migration.yml`'s shape) so the script can
    persist rows to migration_log on a real failure path."""
    import yaml  # type: ignore[import-untyped]

    workflow_path = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "delisting_check.yml"
    )
    with workflow_path.open("r") as f:
        workflow = yaml.safe_load(f)

    jobs = workflow["jobs"]
    job = jobs["detect-delistings"]
    assert "postgres" in job["services"]
    assert job["services"]["postgres"]["image"].startswith("postgres:")
    assert "DATABASE_URL" in job["env"]


def test_yahoo_finance_client_surfaces_tradeable(monkeypatch):
    """AC2: `src/yahoo_finance_client.py` exposes the new
    `tradeable` key — True/False when the API surfaces it, None
    when absent."""
    import yahoo_finance_client

    # --- present, True ---
    meta = {"currency": "INR", "regularMarketPrice": 1.0, "tradeable": True}
    body = {
        "chart": {
            "result": [{"meta": meta, "indicators": {"quote": [{}]}}],
            "error": None,
        }
    }

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return body

    captured: dict = {}

    def _fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        return _FakeResp()

    monkeypatch.setattr(api_error_logging.requests, "get", _fake_get)

    quote = yahoo_finance_client.fetch_yahoo_finance_quote("RELIANCE.NS")
    assert "tradeable" in quote
    assert quote["tradeable"] is True

    # --- present, False (delisted/suspended) ---
    meta["tradeable"] = False
    quote = yahoo_finance_client.fetch_yahoo_finance_quote("RELIANCE.NS")
    assert quote["tradeable"] is False

    # --- absent (API doesn't surface it) → honest None ---
    del meta["tradeable"]
    quote = yahoo_finance_client.fetch_yahoo_finance_quote("RELIANCE.NS")
    assert quote["tradeable"] is None


# --- __main__: invoking the script runs the real pipeline ----------------


@requires_postgres
def test_script_runs_via_python_m(infra, monkeypatch, holdings_table, tmp_path, capsys):
    """End-to-end smoke: `python -m scripts.detect_delisted_stocks`
    exits 0 and prints a well-formed summary line.

    This runs as a genuine child process, so the parent test's
    `monkeypatch`es of `fetch_yahoo_finance_quote` / `_HOLDINGS_TABLE`
    do not cross the process boundary -- the subprocess calls the
    real Yahoo Finance client against whatever symbols are actually
    present in the shared real `holdings` table (accumulated by every
    other test/story that has run against this same Postgres). That
    means exact counts (`symbols_checked=N`) are NOT something this
    test can honestly assert -- it would be measuring incidental
    external state, not this script's behaviour. What IS genuinely
    testable here: the script runs to completion, exits 0, and prints
    a summary line with the documented shape."""
    repo_root = Path(__file__).resolve().parent.parent

    env = os.environ.copy()
    env["DELISTING_DRY_RUN"] = "false"
    env["PYTHONPATH"] = f"{repo_root / 'src'}:{repo_root}"

    result = subprocess.run(
        [sys.executable, "-m", "scripts.detect_delisted_stocks"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"script exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert re.search(
        r"^(APPLIED|DRY-RUN): symbols_checked=\d+ changes_logged=\d+ "
        r"skipped_api_error=\d+ skipped_no_tradeable=\d+\s*$",
        result.stdout,
        re.MULTILINE,
    ), f"unexpected summary line shape: {result.stdout!r}"