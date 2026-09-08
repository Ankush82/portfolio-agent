"""Integration test for the full synchronize_portfolio() lifecycle
(STORY-SYNC-12): sync empty DB (all added) -> sync same data again (all
unchanged) -> modify some broker data (some updated) -> remove some
broker data (some removed) -> sync with empty broker data (all removed).

Real Postgres only (DEFAULT_POSTGRES_DSN); no mocks for DB operations,
matching this project's own real-integration-test convention. Skips
cleanly when Postgres isn't reachable, the same convention
test_infrastructure_postgres.py and friends already use.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg
import pytest

from components.c01_user_portfolio import (
    BrokerCredentials,
    BrokerHolding,
    BrokerTransaction,
    DefaultUserPortfolio,
    Portfolio,
    StubBrokerConnector,
    register_broker_connector,
    unregister_broker_connector,
)
from infrastructure_postgres import DEFAULT_POSTGRES_DSN


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(DEFAULT_POSTGRES_DSN, connect_timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="no live Postgres reachable at DEFAULT_POSTGRES_DSN",
)


@pytest.fixture
def _synced_setup():
    up = DefaultUserPortfolio()
    user_id = str(uuid.uuid4())
    with up._infrastructure._connection().cursor() as cursor:
        cursor.execute(
            "INSERT INTO users (id, email) VALUES (%s, %s)",
            (user_id, f"story189-{user_id}@test.com"),
        )
    run_suffix = uuid.uuid4().hex[:8]
    portfolio = Portfolio(id=f"pf-story189-{run_suffix}", user_id=user_id)
    up._infrastructure.upsert_broker_connection(
        user_id=user_id, broker_id="stub", credentials=BrokerCredentials(access_token="tok")
    )
    # Real, unique-per-test-run isin/tx_id -- this suite runs against a
    # real, persistent Postgres with no per-test cleanup (matching this
    # project's existing real-DB test convention), so reusing a fixed
    # literal across runs would make an "added" look like an "updated"
    # once a prior run's row is still sitting in the table.
    isin_a = f"TESTISIN-{run_suffix}-A"
    isin_b = f"TESTISIN-{run_suffix}-B"
    tx_id = f"story189-tx-{run_suffix}"
    yield up, portfolio, isin_a, isin_b, tx_id
    unregister_broker_connector("stub")


def _holding(isin: str, qty: str) -> BrokerHolding:
    return BrokerHolding(
        symbol="AAPL", isin=isin, quantity=Decimal(qty),
        average_price=Decimal("100"), last_price=Decimal("110"),
    )


def _transaction(tx_id: str, isin: str) -> BrokerTransaction:
    return BrokerTransaction(
        broker_transaction_id=tx_id, symbol="AAPL", isin=isin,
        trade_date=date.today(), side="BUY", quantity=Decimal("10"),
        price=Decimal("100"), amount=Decimal("1000"), exchange="NASDAQ",
        segment="EQ", broker_modified_at=datetime.now(timezone.utc), raw={},
    )


def test_sync_empty_db_all_added(_synced_setup):
    up, portfolio, isin_a, isin_b, tx_id = _synced_setup
    register_broker_connector(StubBrokerConnector(
        holdings=[_holding(isin_a, "10")], transactions=[_transaction(tx_id, isin_a)],
    ))

    result = up.synchronize_portfolio(portfolio)

    assert result.success
    assert result.holdings_added == 1
    assert result.transactions_added == 1
    assert result.holdings_unchanged == 0
    assert result.transactions_unchanged == 0


def test_sync_same_data_again_all_unchanged_no_duplicates(_synced_setup):
    up, portfolio, isin_a, isin_b, tx_id = _synced_setup
    register_broker_connector(StubBrokerConnector(
        holdings=[_holding(isin_a, "10")], transactions=[_transaction(tx_id, isin_a)],
    ))
    up.synchronize_portfolio(portfolio)

    result = up.synchronize_portfolio(portfolio)

    assert result.success
    assert result.holdings_added == 0
    assert result.holdings_unchanged == 1
    assert result.transactions_added == 0
    assert result.transactions_unchanged == 1

    snapshot = up.track_portfolio_state(portfolio)
    assert len(snapshot.positions) == 1  # no duplicate row from the second sync


def test_sync_modified_broker_data_marks_updated(_synced_setup):
    up, portfolio, isin_a, isin_b, tx_id = _synced_setup
    register_broker_connector(StubBrokerConnector(
        holdings=[_holding(isin_a, "10")], transactions=[_transaction(tx_id, isin_a)],
    ))
    up.synchronize_portfolio(portfolio)

    register_broker_connector(StubBrokerConnector(
        holdings=[_holding(isin_a, "25")], transactions=[_transaction(tx_id, isin_a)],
    ))
    result = up.synchronize_portfolio(portfolio)

    assert result.holdings_updated == 1
    assert result.holdings_unchanged == 0

    snapshot = up.track_portfolio_state(portfolio)
    assert snapshot.positions[0].holding.quantity == Decimal("25")


def test_sync_removed_broker_holding_marks_removed_not_deleted(_synced_setup):
    up, portfolio, isin_a, isin_b, tx_id = _synced_setup
    register_broker_connector(StubBrokerConnector(
        holdings=[_holding(isin_a, "10"), _holding(isin_b, "5")],
        transactions=[_transaction(tx_id, isin_a)],
    ))
    up.synchronize_portfolio(portfolio)

    register_broker_connector(StubBrokerConnector(
        holdings=[_holding(isin_a, "10")], transactions=[_transaction(tx_id, isin_a)],
    ))
    result = up.synchronize_portfolio(portfolio)

    assert result.holdings_removed == 1
    snapshot = up.track_portfolio_state(portfolio)
    assert len(snapshot.positions) == 1  # the removed one no longer appears


def test_sync_with_empty_broker_response_removes_all_active_records(_synced_setup):
    up, portfolio, isin_a, isin_b, tx_id = _synced_setup
    register_broker_connector(StubBrokerConnector(
        holdings=[_holding(isin_a, "10")], transactions=[_transaction(tx_id, isin_a)],
    ))
    up.synchronize_portfolio(portfolio)

    register_broker_connector(StubBrokerConnector(holdings=[], transactions=[]))
    result = up.synchronize_portfolio(portfolio)

    assert result.holdings_removed == 1
    assert result.transactions_removed == 1
    snapshot = up.track_portfolio_state(portfolio)
    assert snapshot.positions == []
