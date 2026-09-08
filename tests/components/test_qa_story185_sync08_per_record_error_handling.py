"""QA tests for STORY-SYNC-08: per-record error handling.

Acceptance criteria:
  1. Each record upsert is wrapped in try/except.
  2. On exception: record added to failed_records with full context (record_type,
     broker_id, reason, raw_data with the original broker data).
  3. On exception: appropriate *_failed counter incremented.
  4. On exception: processing continues with next record.
  5. SyncResult.success returns False when any records failed.
  6. Unit test simulates one bad record among good ones, verifies others processed.

These tests verify the behaviour directly by:
  - Injecting a broker connector that returns mixed good/bad records.
  - Checking the actual counts, failed_records, and success flag from the
    real synchronize_portfolio() call.
  - Accessing FailedRecord.raw_data correctly: it's the original dataclass
    instance from the broker, so to get the stub marker we access .raw.
"""

import pytest
from decimal import Decimal
from datetime import datetime as dt, timezone

from components.c01_user_portfolio import (
    BrokerHolding,
    BrokerTransaction,
    DefaultUserPortfolio,
    FailedRecord,
    Portfolio,
    SyncResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _InMemoryInfra:
    """Minimal Infrastructure test double used by these integration tests."""

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


# ---------------------------------------------------------------------------
# AC-1: try/except wraps each holding upsert
# ---------------------------------------------------------------------------

def test_bad_holding_causes_holdings_failed_increment():
    """AC-3: when a holding's data causes Holding(...) to raise,
    holdings_failed is incremented."""
    class _ConnectorWithBadHolding:
        broker_id = "test-broker"
        display_name = "Test Broker"

        def fetch_holdings(self, *, credentials):
            # quantity="not-a-number" will cause Holding.__post_init__
            # to raise ValueError via _coerce_quantity_to_decimal.
            return [
                BrokerHolding(
                    symbol="GOOD1",
                    isin="US1111111111",
                    quantity=Decimal("1"),
                    average_price=Decimal("100"),
                    last_price=Decimal("110"),
                    raw={"stub": True},
                ),
                BrokerHolding(
                    symbol="BAD",
                    isin="US0000000001",
                    quantity="not-a-number",  # type: ignore[arg-type]
                    average_price=Decimal("0"),
                    last_price=Decimal("0"),
                    raw={"stub": True},
                ),
            ]

        def fetch_transactions(self, *, credentials, start_date, end_date):
            return []

    infra = _InMemoryInfra()
    infra.store("portfolios", {
        "id": "pf-hf",
        "user_id": "user-hf",
        "broker_connection": {"access_token": "stub", "token_type": "Bearer"},
    })
    portfolio_component = DefaultUserPortfolio(
        infrastructure=infra,
        broker_connector=_ConnectorWithBadHolding(),
    )
    portfolio = Portfolio(id="pf-hf", user_id="user-hf")

    result = portfolio_component.synchronize_portfolio(portfolio)

    # AC-3: holdings_failed counter incremented for the bad record.
    assert result.holdings_failed == 1, (
        f"expected holdings_failed=1, got {result.holdings_failed}"
    )


# ---------------------------------------------------------------------------
# AC-2: record added to failed_records with full context
# ---------------------------------------------------------------------------

def test_bad_holding_in_failed_records_with_full_context():
    """AC-2: a failed holding appears in failed_records with record_type,
    broker_id, reason, and raw_data (the original BrokerHolding dataclass)."""
    class _Connector:
        broker_id = "my-broker"
        display_name = "My Broker"

        def fetch_holdings(self, *, credentials):
            return [
                BrokerHolding(
                    symbol="BAD",
                    isin="US000BAD0001",
                    quantity="bad-qty",
                    average_price=Decimal("0"),
                    last_price=Decimal("0"),
                    raw={"marker": "test-holding-bad"},
                ),
            ]

        def fetch_transactions(self, *, credentials, start_date, end_date):
            return []

    infra = _InMemoryInfra()
    infra.store("portfolios", {
        "id": "pf-fr",
        "user_id": "user-fr",
        "broker_connection": {"access_token": "stub", "token_type": "Bearer"},
    })
    portfolio_component = DefaultUserPortfolio(
        infrastructure=infra,
        broker_connector=_Connector(),
    )
    portfolio = Portfolio(id="pf-fr", user_id="user-fr")

    result = portfolio_component.synchronize_portfolio(portfolio)

    # AC-2: bad record appears in failed_records.
    assert len(result.failed_records) == 1
    bad = result.failed_records[0]
    assert isinstance(bad, FailedRecord), "failed record must be a FailedRecord"

    # AC-2: record_type is "holding"
    assert bad.record_type == "holding", f"expected 'holding', got {bad.record_type!r}"

    # AC-2: broker_id comes from the connector
    assert bad.broker_id == "my-broker", f"expected 'my-broker', got {bad.broker_id!r}"

    # AC-2: reason is a non-empty string describing the error
    assert isinstance(bad.reason, str) and bad.reason, (
        f"reason must be a non-empty string, got {bad.reason!r}"
    )
    # The reason should mention the bad quantity value
    assert "not-a-number" in bad.reason or "bad-qty" in bad.reason, (
        f"reason should mention the bad value, got {bad.reason!r}"
    )

    # AC-2: raw_data is the original BrokerHolding dataclass instance
    # (not the inner dict). Access .raw to get the inner dict marker.
    assert hasattr(bad.raw_data, "raw"), (
        f"raw_data should be the original BrokerHolding dataclass, "
        f"got {type(bad.raw_data).__name__}"
    )
    assert bad.raw_data.raw.get("marker") == "test-holding-bad", (
        f"expected raw_data.raw['marker']=='test-holding-bad', "
        f"got {bad.raw_data.raw.get('marker')!r}"
    )


# ---------------------------------------------------------------------------
# AC-3: transactions_failed counter incremented
# ---------------------------------------------------------------------------

def test_bad_transaction_causes_transactions_failed_increment():
    """AC-3: when a transaction's data causes Transaction(...) to raise,
    transactions_failed is incremented."""
    class _ConnectorWithBadTransaction:
        broker_id = "test-broker-tx"
        display_name = "Test Broker TX"

        def fetch_holdings(self, *, credentials):
            return []

        def fetch_transactions(self, *, credentials, start_date, end_date):
            return [
                BrokerTransaction(
                    broker_transaction_id="tx-good",
                    symbol="AAPL",
                    isin="US0378331005",
                    trade_date=dt(2024, 1, 15).date(),
                    side="BUY",
                    quantity=Decimal("1"),
                    price=Decimal("100"),
                    amount=Decimal("100"),
                    exchange="NASDAQ",
                    segment="EQ",
                    broker_modified_at=dt(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
                    raw={"marker": "good-tx"},
                ),
                BrokerTransaction(
                    broker_transaction_id="tx-bad",
                    symbol="AAPL",
                    isin="US0378331005",
                    trade_date=dt(2024, 2, 10).date(),
                    side="BUY",
                    quantity=Decimal("1"),
                    price=Decimal("100"),
                    amount="not-a-decimal",  # type: ignore[arg-type]
                    exchange="NASDAQ",
                    segment="EQ",
                    broker_modified_at=dt(2024, 2, 10, 12, 0, 0, tzinfo=timezone.utc),
                    raw={"marker": "bad-tx"},
                ),
            ]

    infra = _InMemoryInfra()
    infra.store("portfolios", {
        "id": "pf-tx-failed",
        "user_id": "user-tx-failed",
        "broker_connection": {"access_token": "stub", "token_type": "Bearer"},
    })
    portfolio_component = DefaultUserPortfolio(
        infrastructure=infra,
        broker_connector=_ConnectorWithBadTransaction(),
    )
    portfolio = Portfolio(id="pf-tx-failed", user_id="user-tx-failed")

    result = portfolio_component.synchronize_portfolio(portfolio)

    # AC-3: transactions_failed counter incremented for the bad record.
    assert result.transactions_failed == 1, (
        f"expected transactions_failed=1, got {result.transactions_failed}"
    )


# ---------------------------------------------------------------------------
# AC-4: processing continues with next record
# ---------------------------------------------------------------------------

def test_processing_continues_after_bad_holding():
    """AC-4: one bad holding does NOT abort the loop; good holdings
    after it are still added."""
    class _ConnectorWithBadInMiddle:
        broker_id = "cont-broker"
        display_name = "Continue Broker"

        def fetch_holdings(self, *, credentials):
            return [
                BrokerHolding(
                    symbol="GOOD1",
                    isin="US1111111111",
                    quantity=Decimal("1"),
                    average_price=Decimal("100"),
                    last_price=Decimal("110"),
                    raw={"position": 1},
                ),
                BrokerHolding(
                    symbol="BAD",
                    isin="US0000000001",
                    quantity="bad",
                    average_price=Decimal("0"),
                    last_price=Decimal("0"),
                    raw={"position": 2},
                ),
                BrokerHolding(
                    symbol="GOOD2",
                    isin="US2222222222",
                    quantity=Decimal("2"),
                    average_price=Decimal("200"),
                    last_price=Decimal("220"),
                    raw={"position": 3},
                ),
            ]

        def fetch_transactions(self, *, credentials, start_date, end_date):
            return []

    infra = _InMemoryInfra()
    infra.store("portfolios", {
        "id": "pf-cont",
        "user_id": "user-cont",
        "broker_connection": {"access_token": "stub", "token_type": "Bearer"},
    })
    portfolio_component = DefaultUserPortfolio(
        infrastructure=infra,
        broker_connector=_ConnectorWithBadInMiddle(),
    )
    portfolio = Portfolio(id="pf-cont", user_id="user-cont")

    result = portfolio_component.synchronize_portfolio(portfolio)

    # AC-4: 2 good holdings were added (GOOD1 and GOOD2).
    assert result.holdings_added == 2, (
        f"expected 2 holdings_added, got {result.holdings_added}; "
        "bad record should not have aborted the loop"
    )

    # AC-4: exactly 1 failed record.
    assert len(result.failed_records) == 1
    assert result.failed_records[0].raw_data.raw.get("position") == 2


def test_processing_continues_after_bad_transaction():
    """AC-4: one bad transaction does NOT abort the loop; good transactions
    after it are still added."""
    class _ConnectorWithBadTxInMiddle:
        broker_id = "cont-tx-broker"
        display_name = "Continue TX Broker"

        def fetch_holdings(self, *, credentials):
            return []

        def fetch_transactions(self, *, credentials, start_date, end_date):
            return [
                BrokerTransaction(
                    broker_transaction_id="tx-001",
                    symbol="AAPL",
                    isin="US0378331005",
                    trade_date=dt(2024, 1, 15).date(),
                    side="BUY",
                    quantity=Decimal("1"),
                    price=Decimal("100"),
                    amount=Decimal("100"),
                    exchange="NASDAQ",
                    segment="EQ",
                    broker_modified_at=dt(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
                    raw={"seq": 1},
                ),
                BrokerTransaction(
                    broker_transaction_id="tx-002",
                    symbol="AAPL",
                    isin="US0378331005",
                    trade_date=dt(2024, 2, 10).date(),
                    side="BUY",
                    quantity=Decimal("1"),
                    price=Decimal("100"),
                    amount="bad-amount",
                    exchange="NASDAQ",
                    segment="EQ",
                    broker_modified_at=dt(2024, 2, 10, 12, 0, 0, tzinfo=timezone.utc),
                    raw={"seq": 2},
                ),
                BrokerTransaction(
                    broker_transaction_id="tx-003",
                    symbol="MSFT",
                    isin="US5949181045",
                    trade_date=dt(2024, 3, 1).date(),
                    side="SELL",
                    quantity=Decimal("1"),
                    price=Decimal("200"),
                    amount=Decimal("200"),
                    exchange="NASDAQ",
                    segment="EQ",
                    broker_modified_at=dt(2024, 3, 1, 12, 0, 0, tzinfo=timezone.utc),
                    raw={"seq": 3},
                ),
            ]

    infra = _InMemoryInfra()
    infra.store("portfolios", {
        "id": "pf-cont-tx",
        "user_id": "user-cont-tx",
        "broker_connection": {"access_token": "stub", "token_type": "Bearer"},
    })
    portfolio_component = DefaultUserPortfolio(
        infrastructure=infra,
        broker_connector=_ConnectorWithBadTxInMiddle(),
    )
    portfolio = Portfolio(id="pf-cont-tx", user_id="user-cont-tx")

    result = portfolio_component.synchronize_portfolio(portfolio)

    # AC-4: 2 good transactions were added.
    assert result.transactions_added == 2, (
        f"expected 2 transactions_added, got {result.transactions_added}; "
        "bad record should not have aborted the loop"
    )

    # AC-4: exactly 1 failed record, and it's the middle (seq=2) one.
    assert len(result.failed_records) == 1
    assert result.failed_records[0].raw_data.raw.get("seq") == 2


# ---------------------------------------------------------------------------
# AC-5: SyncResult.success is False when any records failed
# ---------------------------------------------------------------------------

def test_sync_result_success_false_when_holdings_failed():
    """AC-5: SyncResult.success is False when holdings_failed > 0."""
    class _Connector:
        broker_id = "success-test"
        display_name = "Success Test"

        def fetch_holdings(self, *, credentials):
            return [
                BrokerHolding(
                    symbol="BAD",
                    isin="US0000000001",
                    quantity="nope",
                    average_price=Decimal("0"),
                    last_price=Decimal("0"),
                    raw={"marker": "bad-holding"},
                ),
            ]

        def fetch_transactions(self, *, credentials, start_date, end_date):
            return []

    infra = _InMemoryInfra()
    infra.store("portfolios", {
        "id": "pf-success",
        "user_id": "user-success",
        "broker_connection": {"access_token": "stub", "token_type": "Bearer"},
    })
    portfolio_component = DefaultUserPortfolio(
        infrastructure=infra,
        broker_connector=_Connector(),
    )
    portfolio = Portfolio(id="pf-success", user_id="user-success")

    result = portfolio_component.synchronize_portfolio(portfolio)

    # AC-5: success is False when a holding failed.
    assert result.success is False, (
        "SyncResult.success must be False when holdings_failed > 0"
    )


def test_sync_result_success_false_when_transactions_failed():
    """AC-5: SyncResult.success is False when transactions_failed > 0."""
    class _Connector:
        broker_id = "success-tx-test"
        display_name = "Success TX Test"

        def fetch_holdings(self, *, credentials):
            return []

        def fetch_transactions(self, *, credentials, start_date, end_date):
            return [
                BrokerTransaction(
                    broker_transaction_id="tx-fail",
                    symbol="AAPL",
                    isin="US0378331005",
                    trade_date=dt(2024, 1, 1).date(),
                    side="BUY",
                    quantity=Decimal("1"),
                    price=Decimal("100"),
                    amount="bad",
                    exchange="NASDAQ",
                    segment="EQ",
                    broker_modified_at=dt(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
                    raw={"marker": "bad-tx"},
                ),
            ]

    infra = _InMemoryInfra()
    infra.store("portfolios", {
        "id": "pf-success-tx",
        "user_id": "user-success-tx",
        "broker_connection": {"access_token": "stub", "token_type": "Bearer"},
    })
    portfolio_component = DefaultUserPortfolio(
        infrastructure=infra,
        broker_connector=_Connector(),
    )
    portfolio = Portfolio(id="pf-success-tx", user_id="user-success-tx")

    result = portfolio_component.synchronize_portfolio(portfolio)

    # AC-5: success is False when a transaction failed.
    assert result.success is False, (
        "SyncResult.success must be False when transactions_failed > 0"
    )


# ---------------------------------------------------------------------------
# AC-6: mixed bad/good — all criteria together
# ---------------------------------------------------------------------------

def test_mixed_good_and_bad_records_all_criteria():
    """AC-1 through AC-6: one bad holding AND one bad transaction among
    good ones, verifying all acceptance criteria in a single pass.

    - Both *_failed counters are incremented.
    - failed_records has 2 entries.
    - Good records from both types are still added.
    - success is False.
    """
    class _MixedConnector:
        broker_id = "mixed-broker"
        display_name = "Mixed Broker"

        def fetch_holdings(self, *, credentials):
            return [
                BrokerHolding(
                    symbol="H_GOOD",
                    isin="US1111111111",
                    quantity=Decimal("10"),
                    average_price=Decimal("100"),
                    last_price=Decimal("110"),
                    raw={"seq": "h1"},
                ),
                BrokerHolding(
                    symbol="H_BAD",
                    isin="US0000000001",
                    quantity="bad",
                    average_price=Decimal("0"),
                    last_price=Decimal("0"),
                    raw={"seq": "h2"},
                ),
                BrokerHolding(
                    symbol="H_GOOD2",
                    isin="US2222222222",
                    quantity=Decimal("20"),
                    average_price=Decimal("200"),
                    last_price=Decimal("210"),
                    raw={"seq": "h3"},
                ),
            ]

        def fetch_transactions(self, *, credentials, start_date, end_date):
            return [
                BrokerTransaction(
                    broker_transaction_id="tx-good",
                    symbol="AAPL",
                    isin="US0378331005",
                    trade_date=dt(2024, 1, 15).date(),
                    side="BUY",
                    quantity=Decimal("1"),
                    price=Decimal("100"),
                    amount=Decimal("100"),
                    exchange="NASDAQ",
                    segment="EQ",
                    broker_modified_at=dt(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
                    raw={"seq": "t1"},
                ),
                BrokerTransaction(
                    broker_transaction_id="tx-bad",
                    symbol="AAPL",
                    isin="US0378331005",
                    trade_date=dt(2024, 2, 10).date(),
                    side="BUY",
                    quantity=Decimal("1"),
                    price=Decimal("100"),
                    amount="bad-amount",
                    exchange="NASDAQ",
                    segment="EQ",
                    broker_modified_at=dt(2024, 2, 10, 12, 0, 0, tzinfo=timezone.utc),
                    raw={"seq": "t2"},
                ),
            ]

    infra = _InMemoryInfra()
    infra.store("portfolios", {
        "id": "pf-mixed",
        "user_id": "user-mixed",
        "broker_connection": {"access_token": "stub", "token_type": "Bearer"},
    })
    portfolio_component = DefaultUserPortfolio(
        infrastructure=infra,
        broker_connector=_MixedConnector(),
    )
    portfolio = Portfolio(id="pf-mixed", user_id="user-mixed")

    result = portfolio_component.synchronize_portfolio(portfolio)

    # AC-3: *_failed counters
    assert result.holdings_failed == 1, f"holdings_failed: expected 1, got {result.holdings_failed}"
    assert result.transactions_failed == 1, f"transactions_failed: expected 1, got {result.transactions_failed}"

    # AC-4: processing continues — good records added
    assert result.holdings_added == 2, f"holdings_added: expected 2, got {result.holdings_added}"
    assert result.transactions_added == 1, f"transactions_added: expected 1, got {result.transactions_added}"

    # AC-2: failed_records has both failures
    assert len(result.failed_records) == 2

    # Separate holdings and transactions failures
    holding_failures = [r for r in result.failed_records if r.record_type == "holding"]
    tx_failures = [r for r in result.failed_records if r.record_type == "transaction"]
    assert len(holding_failures) == 1
    assert len(tx_failures) == 1

    # AC-2: full context on each failure
    hf = holding_failures[0]
    assert hf.broker_id == "mixed-broker"
    assert isinstance(hf.reason, str) and hf.reason
    assert hf.raw_data.raw.get("seq") == "h2"

    tf = tx_failures[0]
    assert tf.broker_id == "mixed-broker"
    assert isinstance(tf.reason, str) and tf.reason
    assert tf.raw_data.raw.get("seq") == "t2"

    # AC-5: success is False
    assert result.success is False, "success must be False when any record failed"


# ---------------------------------------------------------------------------
# AC-1: try/except wraps each transaction upsert
# ---------------------------------------------------------------------------

def test_bad_transaction_in_failed_records_with_full_context():
    """AC-2: a failed transaction appears in failed_records with record_type,
    broker_id, reason, and raw_data (the original BrokerTransaction dataclass)."""
    class _Connector:
        broker_id = "tx-broker-ctx"
        display_name = "TX Broker Ctx"

        def fetch_holdings(self, *, credentials):
            return []

        def fetch_transactions(self, *, credentials, start_date, end_date):
            return [
                BrokerTransaction(
                    broker_transaction_id="tx-bad-ctx",
                    symbol="AAPL",
                    isin="US0378331005",
                    trade_date=dt(2024, 1, 1).date(),
                    side="BUY",
                    quantity=Decimal("1"),
                    price=Decimal("100"),
                    amount="not-a-decimal",
                    exchange="NASDAQ",
                    segment="EQ",
                    broker_modified_at=dt(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
                    raw={"marker": "test-tx-bad", "source": "ctx-test"},
                ),
            ]

    infra = _InMemoryInfra()
    infra.store("portfolios", {
        "id": "pf-tx-ctx",
        "user_id": "user-tx-ctx",
        "broker_connection": {"access_token": "stub", "token_type": "Bearer"},
    })
    portfolio_component = DefaultUserPortfolio(
        infrastructure=infra,
        broker_connector=_Connector(),
    )
    portfolio = Portfolio(id="pf-tx-ctx", user_id="user-tx-ctx")

    result = portfolio_component.synchronize_portfolio(portfolio)

    # AC-2: one failed record in the list
    assert len(result.failed_records) == 1
    bad = result.failed_records[0]
    assert isinstance(bad, FailedRecord), "failed record must be a FailedRecord"

    # AC-2: record_type is "transaction"
    assert bad.record_type == "transaction", f"expected 'transaction', got {bad.record_type!r}"

    # AC-2: broker_id comes from the connector
    assert bad.broker_id == "tx-broker-ctx", f"expected 'tx-broker-ctx', got {bad.broker_id!r}"

    # AC-2: reason is a non-empty string describing the error
    assert isinstance(bad.reason, str) and bad.reason, (
        f"reason must be a non-empty string, got {bad.reason!r}"
    )
    # The reason should mention the bad amount value
    assert "not-a-decimal" in bad.reason or "Decimal" in bad.reason, (
        f"reason should mention the bad value or Decimal type, got {bad.reason!r}"
    )

    # AC-2: raw_data is the original BrokerTransaction dataclass instance
    # (not the inner dict). Access .raw to get the inner dict marker.
    assert hasattr(bad.raw_data, "raw"), (
        f"raw_data should be the original BrokerTransaction dataclass, "
        f"got {type(bad.raw_data).__name__}"
    )
    assert bad.raw_data.raw.get("marker") == "test-tx-bad", (
        f"expected raw_data.raw['marker']=='test-tx-bad', "
        f"got {bad.raw_data.raw.get('marker')!r}"
    )
    assert bad.raw_data.raw.get("source") == "ctx-test", (
        f"expected raw_data.raw['source']=='ctx-test', "
        f"got {bad.raw_data.raw.get('source')!r}"
    )
