"""QA tests for STORY-178: SyncResult and FailedRecord data structures.

Acceptance criteria (from STORY-178 brief):
  1. SyncResult dataclass exists with all *_added, *_updated, *_unchanged,
     *_removed, *_failed counters for holdings and transactions.
  2. FailedRecord dataclass exists with record_type, broker_id, reason,
     and raw_data fields.
  3. SyncResult has success property returning True when no failures occurred.
  4. SyncResult has has_changes property returning True when any records were
     added/updated/removed.
  5. SyncResult has sync_started_at, sync_completed_at, and duration_ms fields.
  6. Unit tests verify SyncResult construction and property logic.

These tests exercise the data structures in isolation (direct construction)
and in integration (via DefaultUserPortfolio.synchronize_portfolio).
"""

import pytest
from datetime import datetime, timezone

from components.c01_user_portfolio import (
    FailedRecord,
    SyncResult,
)


# ---------------------------------------------------------------------------
# AC-1: SyncResult has all counter fields
# ---------------------------------------------------------------------------

def test_sync_result_has_all_holdings_counter_fields():
    """AC-1: SyncResult exposes holdings_added, holdings_updated,
    holdings_unchanged, holdings_removed, holdings_failed fields."""
    result = SyncResult(portfolio_id="pf-1")
    for attr in (
        "holdings_added",
        "holdings_updated",
        "holdings_unchanged",
        "holdings_removed",
        "holdings_failed",
    ):
        assert hasattr(result, attr), f"SyncResult missing holdings counter: {attr}"


def test_sync_result_has_all_transactions_counter_fields():
    """AC-1: SyncResult exposes transactions_added, transactions_updated,
    transactions_unchanged, transactions_removed, transactions_failed fields."""
    result = SyncResult(portfolio_id="pf-1")
    for attr in (
        "transactions_added",
        "transactions_updated",
        "transactions_unchanged",
        "transactions_removed",
        "transactions_failed",
    ):
        assert hasattr(result, attr), f"SyncResult missing transactions counter: {attr}"


def test_sync_result_counters_default_to_zero():
    """AC-1: Counters default to 0 (int), not None or some other sentinel."""
    result = SyncResult(portfolio_id="pf-1")
    for attr in (
        "holdings_added", "holdings_updated", "holdings_unchanged",
        "holdings_removed", "holdings_failed",
        "transactions_added", "transactions_updated", "transactions_unchanged",
        "transactions_removed", "transactions_failed",
    ):
        value = getattr(result, attr)
        assert isinstance(value, int), f"{attr} must be int, got {type(value).__name__}"
        assert value == 0, f"{attr} default must be 0, got {value}"


def test_sync_result_counters_accept_non_zero_values():
    """AC-1: Counters can be set to non-zero values to reflect real sync results."""
    result = SyncResult(
        portfolio_id="pf-1",
        holdings_added=3,
        holdings_updated=1,
        holdings_unchanged=5,
        holdings_removed=0,
        holdings_failed=2,
        transactions_added=10,
        transactions_updated=0,
        transactions_unchanged=2,
        transactions_removed=1,
        transactions_failed=0,
    )
    assert result.holdings_added == 3
    assert result.holdings_updated == 1
    assert result.holdings_unchanged == 5
    assert result.holdings_removed == 0
    assert result.holdings_failed == 2
    assert result.transactions_added == 10
    assert result.transactions_updated == 0
    assert result.transactions_unchanged == 2
    assert result.transactions_removed == 1
    assert result.transactions_failed == 0


# ---------------------------------------------------------------------------
# AC-2: FailedRecord has the required fields
# ---------------------------------------------------------------------------

def test_failed_record_has_required_fields():
    """AC-2: FailedRecord exposes record_type, broker_id, reason, raw_data."""
    fr = FailedRecord(
        record_type="holding",
        broker_id="upstox",
        reason="missing trading_symbol",
        raw_data={"isin": "INE002A01018"},
    )
    assert fr.record_type == "holding"
    assert fr.broker_id == "upstox"
    assert fr.reason == "missing trading_symbol"
    assert fr.raw_data == {"isin": "INE002A01018"}


def test_failed_record_broker_id_can_be_none():
    """AC-2: broker_id is Optional[str] — None for manual-entry failures."""
    fr = FailedRecord(
        record_type="transaction",
        broker_id=None,
        reason="invalid amount: not a number",
        raw_data={},
    )
    assert fr.broker_id is None


def test_failed_record_raw_data_defaults_to_empty_dict():
    """AC-2: raw_data has a sensible default (empty dict, not required)."""
    fr = FailedRecord(
        record_type="holding",
        broker_id="stub",
        reason="parse error",
    )
    assert fr.raw_data == {}


def test_failed_record_in_list_on_sync_result():
    """AC-2: SyncResult.failed_records is a list of FailedRecord."""
    fr1 = FailedRecord(record_type="holding", broker_id="upstox", reason="bad data")
    result = SyncResult(portfolio_id="pf-1", failed_records=[fr1])
    assert result.failed_records == [fr1]
    assert isinstance(result.failed_records, list)
    assert all(isinstance(r, FailedRecord) for r in result.failed_records)


# ---------------------------------------------------------------------------
# AC-3: success property — True when no failures
# ---------------------------------------------------------------------------

def test_sync_result_success_true_when_no_failures():
    """AC-3: success is True when holdings_failed == 0 and
    transactions_failed == 0 (even if other counters are non-zero)."""
    result = SyncResult(
        portfolio_id="pf-1",
        holdings_added=5,
        holdings_updated=2,
        holdings_failed=0,
        transactions_added=10,
        transactions_failed=0,
    )
    assert result.success is True


def test_sync_result_success_false_when_holdings_failed():
    """AC-3: success is False when holdings_failed > 0."""
    result = SyncResult(
        portfolio_id="pf-1",
        holdings_added=5,
        holdings_failed=1,
        transactions_failed=0,
    )
    assert result.success is False


def test_sync_result_success_false_when_transactions_failed():
    """AC-3: success is False when transactions_failed > 0."""
    result = SyncResult(
        portfolio_id="pf-1",
        holdings_failed=0,
        transactions_added=10,
        transactions_failed=1,
    )
    assert result.success is False


def test_sync_result_success_false_when_both_failures_present():
    """AC-3: success is False when both holdings_failed and
    transactions_failed are non-zero."""
    result = SyncResult(
        portfolio_id="pf-1",
        holdings_failed=2,
        transactions_failed=3,
    )
    assert result.success is False


def test_sync_result_success_true_when_only_failures_are_zero():
    """AC-3: success is True when both counters are explicitly zero."""
    result = SyncResult(
        portfolio_id="pf-1",
        holdings_failed=0,
        transactions_failed=0,
    )
    assert result.success is True


# ---------------------------------------------------------------------------
# AC-4: has_changes property — True when any records were added/updated/removed
# ---------------------------------------------------------------------------

def test_sync_result_has_changes_true_when_holdings_added():
    """AC-4: has_changes is True when holdings_added > 0."""
    result = SyncResult(portfolio_id="pf-1", holdings_added=1)
    assert result.has_changes is True


def test_sync_result_has_changes_true_when_holdings_updated():
    """AC-4: has_changes is True when holdings_updated > 0."""
    result = SyncResult(portfolio_id="pf-1", holdings_updated=1)
    assert result.has_changes is True


def test_sync_result_has_changes_true_when_holdings_removed():
    """AC-4: has_changes is True when holdings_removed > 0."""
    result = SyncResult(portfolio_id="pf-1", holdings_removed=1)
    assert result.has_changes is True


def test_sync_result_has_changes_true_when_transactions_added():
    """AC-4: has_changes is True when transactions_added > 0."""
    result = SyncResult(portfolio_id="pf-1", transactions_added=1)
    assert result.has_changes is True


def test_sync_result_has_changes_true_when_transactions_updated():
    """AC-4: has_changes is True when transactions_updated > 0."""
    result = SyncResult(portfolio_id="pf-1", transactions_updated=1)
    assert result.has_changes is True


def test_sync_result_has_changes_true_when_transactions_removed():
    """AC-4: has_changes is True when transactions_removed > 0."""
    result = SyncResult(portfolio_id="pf-1", transactions_removed=1)
    assert result.has_changes is True


def test_sync_result_has_changes_false_when_only_unchanged_and_failed():
    """AC-4: has_changes is False when only unchanged/failed counters are
    non-zero — "failed" is not "changed" per the dataclass docstring."""
    result = SyncResult(
        portfolio_id="pf-1",
        holdings_unchanged=5,
        holdings_failed=2,
        transactions_unchanged=3,
        transactions_failed=1,
    )
    assert result.has_changes is False


def test_sync_result_has_changes_false_when_all_counters_are_zero():
    """AC-4: has_changes is False when every counter is zero
    (fully-unmodified sync)."""
    result = SyncResult(portfolio_id="pf-1")
    assert result.has_changes is False


def test_sync_result_has_changes_not_affected_by_failed():
    """AC-4: a sync that only had failures but no structural changes
    still has has_changes=False — "failed" is not "changed"."""
    result = SyncResult(
        portfolio_id="pf-1",
        holdings_failed=3,
        transactions_failed=1,
        # No adds/updates/removes anywhere
    )
    assert result.has_changes is False


# ---------------------------------------------------------------------------
# AC-5: timing fields — sync_started_at, sync_completed_at, duration_ms
# ---------------------------------------------------------------------------

def test_sync_result_has_sync_started_at_field():
    """AC-5: sync_started_at is a settable field on SyncResult."""
    ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    result = SyncResult(portfolio_id="pf-1", sync_started_at=ts)
    assert result.sync_started_at == ts


def test_sync_result_has_sync_completed_at_field():
    """AC-5: sync_completed_at is a settable field on SyncResult."""
    ts = datetime(2025, 6, 1, 12, 5, 0, tzinfo=timezone.utc)
    result = SyncResult(portfolio_id="pf-1", sync_completed_at=ts)
    assert result.sync_completed_at == ts


def test_sync_result_duration_ms_computed_from_timestamps():
    """AC-5: duration_ms = (sync_completed_at - sync_started_at) in ms."""
    from datetime import timedelta
    start = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    end   = datetime(2025, 6, 1, 12, 0, 3, 500000, tzinfo=timezone.utc)
    result = SyncResult(
        portfolio_id="pf-1",
        sync_started_at=start,
        sync_completed_at=end,
    )
    assert result.duration_ms == 3500  # 3.5 seconds = 3500 ms


def test_sync_result_duration_ms_exact_whole_milliseconds():
    """AC-5: duration_ms is an int, not a float."""
    from datetime import timedelta
    start = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    end   = datetime(2025, 6, 1, 12, 0, 1, 234000, tzinfo=timezone.utc)
    result = SyncResult(
        portfolio_id="pf-1",
        sync_started_at=start,
        sync_completed_at=end,
    )
    assert result.duration_ms == 1234
    assert isinstance(result.duration_ms, int)


def test_sync_result_duration_ms_zero_for_zero_elapsed():
    """AC-5: duration_ms is 0 when start == end (instantaneous sync)."""
    ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    result = SyncResult(
        portfolio_id="pf-1",
        sync_started_at=ts,
        sync_completed_at=ts,
    )
    assert result.duration_ms == 0


def test_sync_result_duration_ms_none_when_started_at_is_none():
    """AC-5: duration_ms is None when sync_started_at is not set."""
    result = SyncResult(portfolio_id="pf-1", sync_completed_at=datetime.now(timezone.utc))
    assert result.duration_ms is None


def test_sync_result_duration_ms_none_when_completed_at_is_none():
    """AC-5: duration_ms is None when sync_completed_at is not set."""
    result = SyncResult(portfolio_id="pf-1", sync_started_at=datetime.now(timezone.utc))
    assert result.duration_ms is None


def test_sync_result_duration_ms_none_when_both_timestamps_missing():
    """AC-5: duration_ms is None when neither timestamp is set."""
    result = SyncResult(portfolio_id="pf-1")
    assert result.duration_ms is None


def test_sync_result_duration_ms_consistent_with_timestamps():
    """AC-5: duration_ms derived from (completed - started) is always
    consistent with the two timestamps (never pre-computed)."""
    from datetime import timedelta
    # Test a variety of intervals
    for seconds in (0, 1, 30, 60, 3600, 86400):
        start = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(seconds=seconds)
        result = SyncResult(
            portfolio_id="pf-1",
            sync_started_at=start,
            sync_completed_at=end,
        )
        assert result.duration_ms == seconds * 1000


# ---------------------------------------------------------------------------
# AC-6: Full pipeline — DefaultUserPortfolio.synchronize_portfolio
# returns a properly-constructed SyncResult
# ---------------------------------------------------------------------------

def test_synchronize_portfolio_returns_sync_result_with_timestamps():
    """AC-5/AC-6: synchronize_portfolio populates sync_started_at and
    sync_completed_at on the returned SyncResult."""
    from components.c01_user_portfolio import (
        DefaultUserPortfolio,
        PlaceholderBrokerConnector,
        Portfolio,
    )
    infra = _InMemoryInfra()
    infra.store("portfolios", {
        "id": "pf-timing",
        "user_id": "user-1",
        "broker_connection": {"access_token": "stub-token", "token_type": "Bearer"},
    })
    portfolio_component = DefaultUserPortfolio(
        infrastructure=infra,
        broker_connector=PlaceholderBrokerConnector(),
    )
    portfolio = Portfolio(id="pf-timing", user_id="user-1")

    result = portfolio_component.synchronize_portfolio(portfolio)

    assert result.sync_started_at is not None
    assert result.sync_completed_at is not None
    assert isinstance(result.sync_started_at, datetime)
    assert isinstance(result.sync_completed_at, datetime)


def test_synchronize_portfolio_duration_ms_is_non_negative():
    """AC-5: duration_ms is always >= 0 after a real synchronize_portfolio call."""
    from components.c01_user_portfolio import (
        DefaultUserPortfolio,
        PlaceholderBrokerConnector,
        Portfolio,
    )
    infra = _InMemoryInfra()
    infra.store("portfolios", {
        "id": "pf-dur",
        "user_id": "user-1",
        "broker_connection": {"access_token": "stub-token", "token_type": "Bearer"},
    })
    portfolio_component = DefaultUserPortfolio(
        infrastructure=infra,
        broker_connector=PlaceholderBrokerConnector(),
    )
    portfolio = Portfolio(id="pf-dur", user_id="user-1")

    result = portfolio_component.synchronize_portfolio(portfolio)

    assert result.duration_ms is not None
    assert result.duration_ms >= 0


def test_synchronize_portfolio_has_failed_records_list_attribute():
    """AC-2/AC-6: the returned SyncResult always has a failed_records
    list (empty when nothing failed)."""
    from components.c01_user_portfolio import (
        DefaultUserPortfolio,
        PlaceholderBrokerConnector,
        Portfolio,
    )
    infra = _InMemoryInfra()
    infra.store("portfolios", {
        "id": "pf-failed",
        "user_id": "user-1",
        "broker_connection": {"access_token": "stub-token", "token_type": "Bearer"},
    })
    portfolio_component = DefaultUserPortfolio(
        infrastructure=infra,
        broker_connector=PlaceholderBrokerConnector(),
    )
    portfolio = Portfolio(id="pf-failed", user_id="user-1")

    result = portfolio_component.synchronize_portfolio(portfolio)

    assert hasattr(result, "failed_records")
    assert isinstance(result.failed_records, list)


# ---------------------------------------------------------------------------
# STORY-SYNC-08: per-record error handling
# ---------------------------------------------------------------------------

def test_per_record_error_handling_one_bad_holding_others_succeed():
    """AC-STORY-SYNC-08: one bad holding among good ones is skipped and
    logged, but the remaining holdings are processed normally.

    Verifies:
      - holdings_failed counter is incremented for the bad record.
      - bad record appears in failed_records with full context (record_type,
        broker_id, reason, raw_data).
      - remaining good holdings are still added (holdings_added > 0).
      - processing continues without aborting.
      - SyncResult.success is False when any record failed.
    """
    from decimal import Decimal
    from components.c01_user_portfolio import (
        BrokerHolding,
        DefaultUserPortfolio,
        FailedRecord,
        Portfolio,
    )

    # A connector that returns three holdings: two valid, one that will
    # cause Holding(...) to raise (invalid quantity string).
    class _ConnectorWithOneBadHolding:
        broker_id = "test-broker"
        display_name = "Test Broker"

        def fetch_holdings(self, *, credentials):
            return [
                # Good: valid holding
                BrokerHolding(
                    symbol="AAPL",
                    isin="US0378331005",
                    quantity=Decimal("10"),
                    average_price=Decimal("150.00"),
                    last_price=Decimal("175.00"),
                    raw={"stub": True},
                ),
                # Bad: quantity="not-a-number" will cause
                # _coerce_quantity_to_decimal to raise ValueError inside
                # Holding.__post_init__.
                BrokerHolding(
                    symbol="BAD",
                    isin="US0000000001",
                    quantity="not-a-number",  # type: ignore[arg-type]
                    average_price=Decimal("0"),
                    last_price=Decimal("0"),
                    raw={"stub": True, "should_fail": True},
                ),
                # Good: another valid holding after the bad one
                BrokerHolding(
                    symbol="MSFT",
                    isin="US5949181045",
                    quantity=Decimal("5"),
                    average_price=Decimal("300.00"),
                    last_price=Decimal("310.00"),
                    raw={"stub": True},
                ),
            ]

        def fetch_transactions(self, *, credentials, start_date, end_date):
            return []

    infra = _InMemoryInfra()
    # Seed a portfolio record so _load_credentials finds broker credentials.
    infra.store("portfolios", {
        "id": "pf-per-record",
        "user_id": "user-per-record",
        "broker_connection": {"access_token": "stub-token", "token_type": "Bearer"},
    })
    portfolio_component = DefaultUserPortfolio(
        infrastructure=infra,
        broker_connector=_ConnectorWithOneBadHolding(),
    )
    portfolio = Portfolio(id="pf-per-record", user_id="user-per-record")

    result = portfolio_component.synchronize_portfolio(portfolio)

    # AC: processing continues — 2 holdings were added (the good ones).
    assert result.holdings_added == 2, (
        f"expected 2 successful holdings, got {result.holdings_added}; "
        "bad record should not have aborted the loop"
    )

    # AC: holdings_failed counter incremented for the bad record.
    assert result.holdings_failed == 1, (
        f"expected holdings_failed=1, got {result.holdings_failed}"
    )

    # AC: bad record appears in failed_records with full context.
    assert len(result.failed_records) == 1, (
        f"expected 1 failed record, got {len(result.failed_records)}"
    )
    bad = result.failed_records[0]
    assert isinstance(bad, FailedRecord), "failed record must be a FailedRecord"
    assert bad.record_type == "holding", f"expected record_type='holding', got {bad.record_type!r}"
    assert bad.broker_id == "test-broker", f"expected broker_id='test-broker', got {bad.broker_id!r}"
    assert "not-a-number" in bad.reason, f"expected reason to mention the error, got {bad.reason!r}"
    assert "should_fail" in bad.raw_data.raw, "raw_data.raw must contain the original broker record"

    # AC: SyncResult.success is False when any records failed.
    assert result.success is False, (
        "SyncResult.success must be False when holdings_failed > 0"
    )


def test_per_record_error_handling_continues_after_bad_transaction():
    """AC-STORY-SYNC-08: same as the holding test but for transactions.

    One bad transaction among good ones is skipped, but processing continues
    and the remaining transactions are added. transactions_failed is incremented.
    """
    from decimal import Decimal
    from datetime import datetime as dt, timezone
    from components.c01_user_portfolio import (
        BrokerTransaction,
        DefaultUserPortfolio,
        FailedRecord,
        Portfolio,
    )

    class _ConnectorWithOneBadTransaction:
        broker_id = "test-broker-tx"
        display_name = "Test Broker TX"

        def fetch_holdings(self, *, credentials):
            return []

        def fetch_transactions(self, *, credentials, start_date, end_date):
            return [
                # Good transaction
                BrokerTransaction(
                    broker_transaction_id="tx-good-001",
                    symbol="AAPL",
                    isin="US0378331005",
                    trade_date=dt(2024, 1, 15).date(),
                    side="BUY",
                    quantity=Decimal("2"),
                    price=Decimal("150.00"),
                    amount=Decimal("300.00"),
                    exchange="NASDAQ",
                    segment="EQ",
                    broker_modified_at=dt(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
                    raw={"stub": True},
                ),
                # Bad transaction: invalid amount (string instead of Decimal)
                BrokerTransaction(
                    broker_transaction_id="tx-bad-001",
                    symbol="AAPL",
                    isin="US0378331005",
                    trade_date=dt(2024, 2, 10).date(),
                    side="BUY",
                    quantity=Decimal("1"),
                    price=Decimal("160.00"),
                    amount="not-a-decimal",  # type: ignore[arg-type]
                    exchange="NASDAQ",
                    segment="EQ",
                    broker_modified_at=dt(2024, 2, 10, 12, 0, 0, tzinfo=timezone.utc),
                    raw={"stub": True, "should_fail": True},
                ),
                # Good transaction after the bad one
                BrokerTransaction(
                    broker_transaction_id="tx-good-002",
                    symbol="MSFT",
                    isin="US5949181045",
                    trade_date=dt(2024, 3, 1).date(),
                    side="SELL",
                    quantity=Decimal("1"),
                    price=Decimal("310.00"),
                    amount=Decimal("310.00"),
                    exchange="NASDAQ",
                    segment="EQ",
                    broker_modified_at=dt(2024, 3, 1, 12, 0, 0, tzinfo=timezone.utc),
                    raw={"stub": True},
                ),
            ]

    infra = _InMemoryInfra()
    # Seed a portfolio record so _load_credentials finds broker credentials.
    infra.store("portfolios", {
        "id": "pf-tx-per-record",
        "user_id": "user-tx",
        "broker_connection": {"access_token": "stub-token", "token_type": "Bearer"},
    })
    portfolio_component = DefaultUserPortfolio(
        infrastructure=infra,
        broker_connector=_ConnectorWithOneBadTransaction(),
    )
    portfolio = Portfolio(id="pf-tx-per-record", user_id="user-tx")

    result = portfolio_component.synchronize_portfolio(portfolio)

    # AC: processing continues — at least 2 transactions were added.
    assert result.transactions_added == 2, (
        f"expected 2 successful transactions, got {result.transactions_added}; "
        "bad record should not have aborted the loop"
    )

    # AC: transactions_failed counter incremented for the bad record.
    assert result.transactions_failed == 1, (
        f"expected transactions_failed=1, got {result.transactions_failed}"
    )

    # AC: bad record in failed_records with full context.
    assert len(result.failed_records) == 1
    bad = result.failed_records[0]
    assert isinstance(bad, FailedRecord)
    assert bad.record_type == "transaction"
    assert bad.broker_id == "test-broker-tx"
    assert "Decimal" in bad.reason or "invalid" in bad.reason.lower(), (
        f"expected reason to describe the error, got {bad.reason!r}"
    )
    assert bad.raw_data.raw.get("should_fail") is True

    # AC: SyncResult.success is False when any records failed.
    assert result.success is False


# ---------------------------------------------------------------------------
# Test fixture (duplicated here so this file is fully self-contained;
# matches the pattern used in test_user_portfolio.py)
# ---------------------------------------------------------------------------

class _InMemoryInfra:
    """Minimal Infrastructure test double used by integration tests."""

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, dict]] = {}
        self._next_id = 0

    def store(self, table: str, record: dict) -> str:
        self._next_id += 1
        record_id = str(record.get("id", f"gen-{self._next_id}"))
        self._tables.setdefault(table, {})[record_id] = dict(record, id=record_id)
        return record_id

    def retrieve(self, table: str, id_: str) -> dict | None:
        return self._tables.get(table, {}).get(id_)

    def query(self, table: str, filters: dict) -> list[dict]:
        return [
            r for r in self._tables.get(table, {}).values()
            if all(r.get(k) == v for k, v in filters.items())
        ]

    def get_broker_connection(self, user_id: str, broker_id: str) -> dict | None:
        for record in self._tables.get("broker_connections", {}).values():
            if record.get("user_id") == user_id and record.get("broker_id") == broker_id:
                return record
        return None

    def upsert_broker_connection(self, **kwargs) -> dict:
        self._tables.setdefault("broker_connections", {})
        record_id = f"{kwargs['user_id']}:{kwargs['broker_id']}"
        record = dict(kwargs, id=record_id)
        self._tables["broker_connections"][record_id] = record
        return record

    def mark_broker_connection_error(self, user_id: str, broker_id: str, error: str) -> None:
        conn = self.get_broker_connection(user_id, broker_id)
        if conn:
            conn["last_error"] = error
