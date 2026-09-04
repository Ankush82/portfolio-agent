"""Detect delisted / suspended stocks across every held symbol.

Story STORY-13.

Runs as a scheduled batch job (cron-style, daily at 1 AM UTC via
.github/workflows/delisting_check.yml). Iterates the distinct
`security_id` values across all real user holdings (querying the
`holdings` records store — no hardcoded list), calls
`fetch_yahoo_finance_quote` per symbol, and stores a
`delisting_status` row per symbol (via the generic JSONB
`DefaultInfrastructure.store('delisting_status', {...})` interface,
since this project has no durable per-stock table — see the story's
own grep confirmation).

For each symbol:
  - on `YahooFinanceError`: log + SKIP that one symbol. One bad
    symbol must not crash the rest of the batch (AC: "System handles
    API failures during batch job gracefully").
  - on `quote.get('tradeable') is None`: log + SKIP that one symbol.
    The chart endpoint does not always surface `meta.tradeable`, and
    that absence is honest, not an error — exactly the AC's
    "If API does not provide delisting status, check is skipped
    without error" case.
  - on a real `True`/`False`: diff against the previously-stored
    `delisting_status` row for that symbol. Only on CHANGE (the AC
    explicitly says "log every status CHANGE (not every check)")
    write a new `delisting_status` row AND a `migration_log` row
    carrying a distinct `migration_name` so the change-event is
    queryable separately from schema migrations.

The "distinct log entry type" requirement is satisfied by using a
unique `DELISTING_CHECK_NAME` constant as `migration_log.migration_name`
rather than adding a new table — `migration_log` is already indexed on
`migration_name` (see `idx_migration_log_name_run` in
`infrastructure_postgres._ensure_schema`), so a single query like
`SELECT * FROM migration_log WHERE migration_name = '...'` filters
change-events cleanly away from the schema-migration noise.

Mirrors `scripts/migrate_us_stocks.py`'s shape:
  - real `__main__` block with a printed summary line,
  - `DELISTING_DRY_RUN` env-var gating (case-insensitive true/1/yes),
  - one `migration_log` row per invocation (success/dry-run/failure),
  - real autocommit connection (one implicit transaction per
    statement, so a per-symbol failure does not block subsequent
    symbols).

Both NSE/BSE (`*.NS`, `*.BO`) and US (bare tickers like `AAPL`)
symbols flow through the same `holdings.security_id` field and the
same `fetch_yahoo_finance_quote` call. No special-casing by exchange
group — the story forbids it.

Out of scope:
  - alerting (deliberately out of scope; the change-event row in
    `migration_log` is the persistent artefact another consumer
    would notify on).
  - removing delisted holdings from the user's portfolio (this is
    detection, not remediation).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import uuid
from typing import Iterable

import psycopg

from infrastructure_postgres import DEFAULT_POSTGRES_DSN, DefaultInfrastructure
from yahoo_finance_client import YahooFinanceError, fetch_yahoo_finance_quote

_LOGGER = logging.getLogger("scripts.detect_delisted_stocks")

_TRUTHY = {"true", "1", "yes"}

# Distinct log entry type for delisting change-events (the AC's
# "System ... logs all status changes" requirement, with the
# "queryable separately from schema migrations" qualifier).
# `migration_log` already exists with an index on (migration_name,
# run_at), so a unique value here is the minimum-friction way to
# satisfy "distinct log entry type" without altering schema.
DELISTING_CHECK_NAME = "delisting_status_change_v1"

# Name of the env var that toggles dry-run mode, exported as a real
# constant (mirroring DELISTING_CHECK_NAME) so callers/tests reference
# the same name `_is_dry_run` reads rather than a re-typed literal.
DELISTING_DRY_RUN = "DELISTING_DRY_RUN"

# New logical table name against the generic JSONB `records` table.
# One row per (exchange, symbol); the `id` is the deterministic
# key f"{exchange}:{symbol}" so `retrieve('delisting_status', id)`
# round-trips cleanly. The story confirms there is no durable
# `stocks` table in production, so this uses the generic store /
# retrieve / query interface exactly as instructed.
_DELISTING_STATUS_TABLE = "delisting_status"
_HOLDINGS_TABLE = "holdings"
_SOURCE = "yahoo_tradeable_flag"

# migration_log statuses used by this script.
_LOG_STATUS_SUCCESS = "SUCCESS"
_LOG_STATUS_DRY_RUN = "DRY_RUN"
_LOG_STATUS_FAILED = "FAILED"

# Per-symbol statuses inside the change-event log row's
# `error_message` payload (kept terse — the schema is fixed and
# `error_message` is the only freeform column migration_log has).
_CHANGE_INITIAL = "INITIAL_RECORD"
_CHANGE_TRANSITION = "STATUS_CHANGE"


def _is_dry_run() -> bool:
    raw = os.environ.get(DELISTING_DRY_RUN, "")
    return raw.strip().lower() in _TRUTHY


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def _exchange_for_symbol(symbol: str) -> str:
    """Best-effort exchange label, derived from the symbol suffix
    exactly the way `yahoo_finance_client._derive_exchange_from_symbol`
    does. Returns "" when no `.NS`/`.BO` suffix is present (i.e. a
    bare US ticker) — empty string is the honest answer in that case,
    not a fabricated exchange name."""
    if symbol.endswith(".NS"):
        return "NSE"
    if symbol.endswith(".BO"):
        return "BSE"
    return ""


def _distinct_symbols(infra: DefaultInfrastructure) -> list[str]:
    """Returns the distinct `security_id` values across every
    `holdings` row, regardless of portfolio. Order is not stable —
    call sites should not depend on it. NSE/BSE (.NS, .BO) and US
    (bare) symbols flow through the same `security_id` column;
    neither group is special-cased here (the AC explicitly forbids
    it)."""
    rows = infra.query(_HOLDINGS_TABLE, {})
    seen: dict[str, None] = {}
    for row in rows:
        security_id = row.get("security_id")
        if not security_id:
            continue
        seen[str(security_id)] = None
    return list(seen.keys())


def _previous_status(infra: DefaultInfrastructure, record_id: str) -> dict | None:
    """Returns the previously-stored `delisting_status` row for
    `record_id`, or `None` if there is no prior record (the
    initial-record case, which counts as a CHANGE for logging
    purposes per the AC)."""
    return infra.retrieve(_DELISTING_STATUS_TABLE, record_id)


def _has_status_changed(previous: dict | None, new_is_delisted: bool) -> bool:
    """True iff there's no prior record OR the prior `is_delisted`
    differs from the new one. Per the AC: "log every status CHANGE
    (not every check)"."""
    if previous is None:
        return True
    return bool(previous.get("is_delisted")) != new_is_delisted


def _write_delisting_status_row(
    infra: DefaultInfrastructure,
    *,
    record_id: str,
    symbol: str,
    exchange: str,
    is_delisted: bool,
    checked_at: str,
) -> None:
    """Persists the latest `delisting_status` row, overwriting any
    previous row for the same `(exchange, symbol)` key (the JSONB
    store does an upsert on `table_name, id` — see
    `DefaultInfrastructure.store`)."""
    infra.store(
        _DELISTING_STATUS_TABLE,
        {
            "id": record_id,
            "symbol": symbol,
            "exchange": exchange,
            "is_delisted": is_delisted,
            "checked_at": checked_at,
            "source": _SOURCE,
        },
    )


def _log_change_event(
    cursor: psycopg.Cursor,
    *,
    symbol: str,
    exchange: str,
    previous: dict | None,
    new_is_delisted: bool,
    rows_affected: int,
    dry_run: bool,
) -> None:
    """Inserts one `migration_log` row tagged with
    `DELISTING_CHECK_NAME`, carrying the symbol + transition summary
    in `error_message` (the only freeform text column on the fixed
    `migration_log` schema). The `error_message` column is being
    used as a general-purpose payload here, not as an actual error
    — the `status` column distinguishes real failures from change-
    events."""
    if previous is None:
        change_kind = _CHANGE_INITIAL
        detail = f"symbol={symbol} exchange={exchange!r} is_delisted={new_is_delisted} (no prior record)"
    else:
        change_kind = _CHANGE_TRANSITION
        detail = (
            f"symbol={symbol} exchange={exchange!r} "
            f"is_delisted {previous.get('is_delisted')} -> {new_is_delisted}"
        )
    cursor.execute(
        """
        INSERT INTO migration_log
            (migration_name, run_at, status, rows_affected, error_message, dry_run)
        VALUES (%s, now(), %s, %s, %s, %s)
        """,
        (
            DELISTING_CHECK_NAME,
            change_kind,
            rows_affected,
            detail,
            dry_run,
        ),
    )


def _log_invocation(
    cursor: psycopg.Cursor,
    *,
    status: str,
    rows_affected: int | None,
    error_message: str | None,
    dry_run: bool,
) -> None:
    """One migration_log row per invocation — same posture as
    `scripts/migrate_us_stocks.py`'s `_log`. This is the per-run
    breadcrumb (success/dry-run/failure); the per-symbol change-
    events are written by `_log_change_event` above."""
    cursor.execute(
        """
        INSERT INTO migration_log
            (migration_name, run_at, status, rows_affected, error_message, dry_run)
        VALUES (%s, now(), %s, %s, %s, %s)
        """,
        (DELISTING_CHECK_NAME, status, rows_affected, error_message, dry_run),
    )


def detect_delisted_stocks(
    infrastructure: DefaultInfrastructure | None = None,
    *,
    dry_run: bool | None = None,
    fetcher=None,
) -> dict:
    """One run of the delisting-detection batch.

    `infrastructure` defaults to a fresh `DefaultInfrastructure()`
    (real Postgres-backed JSONB store). `fetcher` defaults to the
    real `fetch_yahoo_finance_quote`; tests inject a fake.
    `dry_run` defaults to the `DELISTING_DRY_RUN` env var.

    Returns a summary dict so callers (including the `__main__`
    block below) can report what happened without re-querying the
    database:

      {
        "symbols_checked": int,           # symbols we actually called Yahoo for
        "changes_logged":   int,          # change-events written to migration_log
        "skipped_api_error": int,         # symbols skipped due to YahooFinanceError
        "skipped_no_tradeable": int,      # symbols skipped because meta.tradeable was absent
      }

    Errors are contained per-symbol: one bad symbol is logged and
    skipped, the run continues with the rest. The summary's
    `skipped_*` counters are the way the caller knows what was
    skipped without re-running.

    On a real failure (Postgres unreachable, etc.) the function
    logs one `FAILED` row to `migration_log` (per
    `scripts/migrate_us_stocks.py`'s contract: every invocation —
    success/dry-run/failure — logs exactly one invocation row) and
    re-raises so the caller / workflow can surface the error.
    """
    if dry_run is None:
        dry_run = _is_dry_run()

    if fetcher is None:
        # Resolved at call time (not bound as a default-argument
        # expression) so tests that monkeypatch the module-level
        # `fetch_yahoo_finance_quote` name actually take effect here.
        # A default bound at def-time would keep pointing at the
        # original real function object forever, silently making
        # every un-monkeypatched real fetcher call during tests hit
        # the live network instead of the test's fake.
        fetcher = fetch_yahoo_finance_quote

    if infrastructure is None:
        infrastructure = DefaultInfrastructure()

    summary = {
        "symbols_checked": 0,
        "changes_logged": 0,
        "skipped_api_error": 0,
        "skipped_no_tradeable": 0,
    }

    # `_connection()` is what triggers _ensure_schema (creates the
    # migration_log table on first use), but we also want a real
    # connection to write the invocation breadcrumb to migration_log
    # even if the symbols query ends up empty. Open the connection
    # explicitly so a Postgres failure surfaces cleanly here.
    connection = infrastructure._connection()

    with connection.cursor() as cursor:
        try:
            symbols = _distinct_symbols(infrastructure)
            checked_at = _now_iso()

            for symbol in symbols:
                exchange = _exchange_for_symbol(symbol)
                record_id = f"{exchange}:{symbol}" if exchange else symbol

                try:
                    quote = fetcher(symbol)
                except YahooFinanceError as exc:
                    # One bad symbol must not crash the batch (AC).
                    _LOGGER.warning(
                        "Skipping %r (exchange=%r): Yahoo Finance error: %s",
                        symbol, exchange or "(unknown)", exc,
                    )
                    summary["skipped_api_error"] += 1
                    continue
                except Exception as exc:
                    # Defence in depth: any other unexpected error
                    # from the fetcher (e.g. a programming bug) is
                    # also contained — the run still proceeds with
                    # the remaining symbols.
                    _LOGGER.exception(
                        "Skipping %r (exchange=%r): unexpected fetcher error",
                        symbol, exchange or "(unknown)",
                    )
                    summary["skipped_api_error"] += 1
                    continue

                tradeable = quote.get("tradeable") if isinstance(quote, dict) else None
                if tradeable is None:
                    # The chart endpoint legitimately doesn't always
                    # surface meta.tradeable. AC5: "If API does not
                    # provide delisting status, check is skipped
                    # without error" — exactly this branch.
                    _LOGGER.info(
                        "Skipping %r (exchange=%r): Yahoo response did not include 'tradeable'.",
                        symbol, exchange or "(unknown)",
                    )
                    summary["skipped_no_tradeable"] += 1
                    continue

                summary["symbols_checked"] += 1
                new_is_delisted = not bool(tradeable)
                previous = _previous_status(infrastructure, record_id)
                if not _has_status_changed(previous, new_is_delisted):
                    continue

                if not dry_run:
                    _write_delisting_status_row(
                        infrastructure,
                        record_id=record_id,
                        symbol=symbol,
                        exchange=exchange,
                        is_delisted=new_is_delisted,
                        checked_at=checked_at,
                    )
                    _log_change_event(
                        cursor,
                        symbol=symbol,
                        exchange=exchange,
                        previous=previous,
                        new_is_delisted=new_is_delisted,
                        rows_affected=1,
                        dry_run=False,
                    )
                else:
                    # Dry-run still logs the change-event breadcrumb
                    # so a monitoring query can see what *would* have
                    # been written — exactly the contract
                    # scripts/migrate_us_stocks.py establishes.
                    _log_change_event(
                        cursor,
                        symbol=symbol,
                        exchange=exchange,
                        previous=previous,
                        new_is_delisted=new_is_delisted,
                        rows_affected=0,
                        dry_run=True,
                    )
                summary["changes_logged"] += 1

            _log_invocation(
                cursor,
                status=_LOG_STATUS_DRY_RUN if dry_run else _LOG_STATUS_SUCCESS,
                rows_affected=summary["changes_logged"],
                error_message=None,
                dry_run=dry_run,
            )
            return summary
        except Exception as exc:
            _log_invocation(
                cursor,
                status=_LOG_STATUS_FAILED,
                rows_affected=None,
                error_message=str(exc),
                dry_run=dry_run,
            )
            raise


# --- helpers surfaced for tests (intentionally minimal) ------------------
#
# These exist so tests can assert the change-detection logic without
# spinning up Postgres; the integration path (real Postgres + real
# fetcher) is covered by `tests/test_detect_delisted_stocks.py`.


def _exchange_for_symbol_for_testing(symbol: str) -> str:
    return _exchange_for_symbol(symbol)


def _has_status_changed_for_testing(previous: dict | None, new_is_delisted: bool) -> bool:
    return _has_status_changed(previous, new_is_delisted)


# Sentinel module attribute so a test can verify the constant is
# importable and stable across refactors. Not used at runtime.
_DELISTING_DETECTION_RUN_ID = str(uuid.uuid4())


if __name__ == "__main__":
    # Module-level logging config so the printed summary line below
    # is the only required output; per-symbol messages go to stderr.
    logging.basicConfig(level=os.environ.get("DELISTING_LOG_LEVEL", "INFO"))
    summary = detect_delisted_stocks()
    mode = "DRY-RUN" if _is_dry_run() else "APPLIED"
    print(
        f"{mode}: symbols_checked={summary['symbols_checked']} "
        f"changes_logged={summary['changes_logged']} "
        f"skipped_api_error={summary['skipped_api_error']} "
        f"skipped_no_tradeable={summary['skipped_no_tradeable']}"
    )