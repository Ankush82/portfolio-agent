"""Integration tests for STORY-14: DefaultUserPortfolio.import_holdings against real Postgres.

These tests exercise the real DefaultInfrastructure and DefaultUserPortfolio
with a fake BrokerConnector, verifying acceptance criteria that require
real transactional behavior:
  - AC-2: running twice leaves exactly 3 rows (replace semantics)
  - AC-3: connector exception mid-import leaves pre-existing rows unchanged (rollback)
  - AC-6: BrokerAuthError sets connection status ERROR with reconnect message, re-raises

Tests against a fresh user_id/broker_id per test to avoid cross-test pollution.
"""

import os
from dataclasses import asdict
from decimal import Decimal

import pytest


def _import_c01():
    """Deferred import so conftest.py (sets BROKER_TOKEN_ENCRYPTION_KEY) runs first."""
    from components.c01_user_portfolio import (
        BrokerAuthError,
        BrokerCredentials,
        BrokerHolding,
        DefaultUserPortfolio,
        HoldingsImportResult,
        StubBrokerConnector,
    )
    from infrastructure_postgres import DefaultInfrastructure
    return (
        BrokerAuthError,
        BrokerCredentials,
        BrokerHolding,
        DefaultUserPortfolio,
        HoldingsImportResult,
        StubBrokerConnector,
        DefaultInfrastructure,
    )


class _FailingAfterNConnector:
    """Test connector that returns N holdings, then raises on first call, then raises on subsequent calls.

    Used to inject a real mid-fetch failure and verify the transaction
    rolls back correctly against the real Postgres infrastructure.
    """

    broker_id = "test-broker"
    display_name = "Test Broker"

    def __init__(self, n_holdings: int = 3):
        self._n = n_holdings
        self._call_count = 0

    def build_authorize_url(self, *, state: str) -> str:
        return "https://test.broker/auth"

    def exchange_auth_code(self, *, code: str):
        # Import inside method to avoid circular import issues during test collection
        from components.c01_user_portfolio import BrokerCredentials
        return BrokerCredentials(access_token="test-token")

    def fetch_holdings(self, *, credentials):
        self._call_count += 1
        if self._call_count > 1:
            raise RuntimeError("simulated mid-fetch database failure during import")
        # Import inside method to avoid circular import issues during test collection
        from components.c01_user_portfolio import BrokerHolding
        return [
            BrokerHolding(
                symbol=f"SYM{i}",
                isin=f"INE{i:09d}",
                quantity=Decimal("10"),
                average_price=Decimal("100.00"),
                last_price=Decimal("110.00"),
                exchange="NSE",
            )
            for i in range(1, self._n + 1)
        ]

    def fetch_transactions(self, *, credentials, start_date, end_date):
        return []


@pytest.fixture
def infra(postgres_dsn):
    """Real DefaultInfrastructure pointing at the test Postgres."""
    from infrastructure_postgres import DefaultInfrastructure
    return DefaultInfrastructure(postgres_dsn=postgres_dsn)


def _seed_connection(infra, user_id: str, broker_id: str) -> None:
    """Insert a CONNECTED broker connection for the given user/broker pair."""
    from broker_token_crypto import encrypt_secret
    from datetime import datetime, timezone

    infra._connection().execute(
        """
        INSERT INTO broker_connections
            (id, user_id, broker_id, broker_user_id,
             access_token_encrypted, token_type,
             status, connected_at, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, broker_id) DO UPDATE SET
            status = EXCLUDED.status,
            access_token_encrypted = EXCLUDED.access_token_encrypted,
            updated_at = EXCLUDED.updated_at
        """,
        (
            f"conn-{user_id}-{broker_id}",
            user_id, broker_id,
            "test-broker-user",
            encrypt_secret("test-access-token"),
            "Bearer",
            "CONNECTED",
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
        ),
    )


def _stored_holdings(infra, user_id: str, broker_id: str) -> list[dict]:
    """Read back the stored holdings for (user_id, broker_id)."""
    rows = infra._connection().execute(
        """
        SELECT symbol, isin, quantity, average_price, last_price, exchange, instrument_id
        FROM broker_holdings
        WHERE user_id = %s AND broker_id = %s
        ORDER BY isin
        """,
        (user_id, broker_id),
    ).fetchall()
    return [
        {
            "symbol": r[0], "isin": r[1], "quantity": r[2],
            "average_price": r[3], "last_price": r[4],
            "exchange": r[5], "instrument_id": r[6],
        }
        for r in rows
    ]


def _connection_status(infra, user_id: str, broker_id: str) -> tuple[str, str | None]:
    """Return (status, last_error) for the stored connection."""
    row = infra._connection().execute(
        "SELECT status, last_error FROM broker_connections WHERE user_id = %s AND broker_id = %s",
        (user_id, broker_id),
    ).fetchone()
    return (row[0], row[1]) if row else ("<none>", None)


def _last_import_at(infra, user_id: str, broker_id: str) -> object:
    """Return last_import_at for the stored connection, or sentinel if not set."""
    row = infra._connection().execute(
        "SELECT last_import_at FROM broker_connections WHERE user_id = %s AND broker_id = %s",
        (user_id, broker_id),
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# AC-2: replace semantics — running import twice leaves exactly 3 rows
# ---------------------------------------------------------------------------

def test_import_twice_replaces_not_duplicates(infra, postgres_dsn):
    """AC-2: Two consecutive imports with different holdings sets leave exactly
    the second set's rows — no duplicates, sold-out holdings disappear."""
    (
        BrokerAuthError, BrokerCredentials, BrokerHolding,
        DefaultUserPortfolio, HoldingsImportResult,
        StubBrokerConnector, DefaultInfrastructure,
    ) = _import_c01()

    user_id = "s14-int-u2"
    broker_id = "s14-int-b2"

    # Seed the connection
    _seed_connection(infra, user_id, broker_id)

    # First batch: 3 holdings
    first_holdings = [
        BrokerHolding(
            symbol=f"SYM{i}", isin=f"INE{i:09d}",
            quantity=Decimal("10"), average_price=Decimal("100.00"),
            last_price=Decimal("110.00"), exchange="NSE",
        )
        for i in range(1, 4)
    ]

    # First import
    connector = StubBrokerConnector(holdings=first_holdings)
    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    result = portfolio.import_holdings(user_id, broker_id)

    assert result.holdings_written == 3, f"first import should write 3, got {result.holdings_written}"
    stored = _stored_holdings(infra, user_id, broker_id)
    assert len(stored) == 3, f"first import should produce 3 rows, got {len(stored)}"

    # Second batch: SYM2 was sold, only 2 holdings returned by broker
    second_holdings = [h for h in first_holdings if h.symbol != "SYM2"]
    assert len(second_holdings) == 2

    connector = StubBrokerConnector(holdings=second_holdings)
    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    result2 = portfolio.import_holdings(user_id, broker_id)

    assert result2.holdings_written == 2, f"second import should write 2, got {result2.holdings_written}"

    stored2 = _stored_holdings(infra, user_id, broker_id)
    assert len(stored2) == 2, f"second import should replace with 2 rows, got {len(stored2)}"

    # SYM2 must be gone — sold-out holdings disappear from our table
    assert all(row["symbol"] != "SYM2" for row in stored2), "SYM2 should have been removed"

    # SYM1 and SYM3 must still be present
    remaining_symbols = {row["symbol"] for row in stored2}
    assert remaining_symbols == {"SYM1", "SYM3"}, f"expected SYM1,SYM3, got {remaining_symbols}"

    # Cleanup
    infra._connection().execute(
        "DELETE FROM broker_connections WHERE user_id = %s AND broker_id = %s",
        (user_id, broker_id),
    )
    infra._connection().execute(
        "DELETE FROM broker_holdings WHERE user_id = %s AND broker_id = %s",
        (user_id, broker_id),
    )


# ---------------------------------------------------------------------------
# AC-3: mid-import exception leaves pre-existing rows unchanged (rollback)
# ---------------------------------------------------------------------------

def test_mid_import_failure_leaves_existing_rows_unchanged(infra, postgres_dsn):
    """AC-3: a connector exception during fetch_holdings leaves the
    pre-existing rows in broker_holdings exactly as they were before
    the failed import attempt — no partial delete, no partial insert."""
    (
        BrokerAuthError, BrokerCredentials, BrokerHolding,
        DefaultUserPortfolio, HoldingsImportResult,
        StubBrokerConnector, DefaultInfrastructure,
    ) = _import_c01()

    user_id = "s14-int-u3"
    broker_id = "s14-int-b3"

    _seed_connection(infra, user_id, broker_id)

    # Pre-existing holdings (simulate a prior successful import)
    infra._connection().execute(
        """
        INSERT INTO broker_holdings
            (user_id, broker_id, isin, symbol, quantity, average_price, last_price, exchange, instrument_id, raw)
        VALUES
            ('s14-int-u3', 's14-int-b3', 'INE000000001', 'OLDSYM1', 10, 100, 110, 'NSE', 'OLD1', '{}'),
            ('s14-int-u3', 's14-int-b3', 'INE000000002', 'OLDSYM2', 20, 200, 220, 'NSE', 'OLD2', '{}'),
            ('s14-int-u3', 's14-int-b3', 'INE000000003', 'OLDSYM3', 30, 300, 330, 'NSE', 'OLD3', '{}')
        """
    )

    assert len(_stored_holdings(infra, user_id, broker_id)) == 3, "setup: should have 3 pre-existing rows"

    # Connector that fails on the second call
    connector = _FailingAfterNConnector(n_holdings=3)

    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    with pytest.raises(RuntimeError):
        portfolio.import_holdings(user_id, broker_id)

    # The pre-existing 3 rows must be completely unchanged — the
    # delete was never committed because the fetch_holdings failure
    # happened before replace_broker_holdings was called.
    remaining = _stored_holdings(infra, user_id, broker_id)
    assert len(remaining) == 3, (
        f"FAILED: expected 3 pre-existing rows unchanged after failed import, got {len(remaining)}. "
        f"Rows: {remaining}"
    )
    symbols = {row["symbol"] for row in remaining}
    assert symbols == {"OLDSYM1", "OLDSYM2", "OLDSYM3"}, (
        f"pre-existing symbols should be unchanged, got {symbols}"
    )

    # Cleanup
    infra._connection().execute(
        "DELETE FROM broker_holdings WHERE user_id = %s AND broker_id = %s",
        (user_id, broker_id),
    )
    infra._connection().execute(
        "DELETE FROM broker_connections WHERE user_id = %s AND broker_id = %s",
        (user_id, broker_id),
    )


# ---------------------------------------------------------------------------
# AC-6: BrokerAuthError marks connection ERROR with reconnect message, re-raises
# ---------------------------------------------------------------------------

def test_broker_auth_error_marks_connection_error_and_reraises(infra, postgres_dsn):
    """AC-6: a BrokerAuthError from fetch_holdings marks the stored
    connection status as ERROR with a reconnect-oriented last_error
    message and then re-raises so the caller knows the import failed."""
    (
        BrokerAuthError, BrokerCredentials, BrokerHolding,
        DefaultUserPortfolio, HoldingsImportResult,
        StubBrokerConnector, DefaultInfrastructure,
    ) = _import_c01()

    user_id = "s14-int-u6"
    broker_id = "s14-int-b6"

    _seed_connection(infra, user_id, broker_id)

    # Verify initial status is CONNECTED
    status_before, error_before = _connection_status(infra, user_id, broker_id)
    assert status_before == "CONNECTED", f"setup: expected CONNECTED, got {status_before}"

    # Connector that raises BrokerAuthError on fetch_holdings
    from components.c01_user_portfolio import BrokerAuthError as ImportedBrokerAuthError
    connector = StubBrokerConnector(holdings=[], raise_on=ImportedBrokerAuthError("token expired"))

    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    with pytest.raises(ImportedBrokerAuthError):
        portfolio.import_holdings(user_id, broker_id)

    status_after, error_after = _connection_status(infra, user_id, broker_id)
    assert status_after == "ERROR", (
        f"FAILED: expected status=ERROR after BrokerAuthError, got {status_after}"
    )
    assert error_after is not None, "last_error must be set after BrokerAuthError"
    assert "reconnect" in error_after.lower(), (
        f"last_error should mention 'reconnect', got: {error_after!r}"
    )
    # The connector's display_name should appear in the message
    assert "stub" in error_after.lower(), (
        f"last_error should include connector display_name, got: {error_after!r}"
    )

    # Cleanup
    infra._connection().execute(
        "DELETE FROM broker_connections WHERE user_id = %s AND broker_id = %s",
        (user_id, broker_id),
    )


# ---------------------------------------------------------------------------
# AC-7: last_import_at updated on success only
# ---------------------------------------------------------------------------

def test_last_import_at_updated_on_success_not_on_failure(infra, postgres_dsn):
    """AC-7: last_import_at on the connection row is set only when
    import_holdings completes successfully — a failing import must not
    update it."""
    (
        BrokerAuthError, BrokerCredentials, BrokerHolding,
        DefaultUserPortfolio, HoldingsImportResult,
        StubBrokerConnector, DefaultInfrastructure,
    ) = _import_c01()

    user_id = "s14-int-u7"
    broker_id = "s14-int-b7"

    _seed_connection(infra, user_id, broker_id)

    # Verify last_import_at is None initially
    assert _last_import_at(infra, user_id, broker_id) is None, "setup: last_import_at should be None"

    # Successful import
    holdings = [
        BrokerHolding(
            symbol="SYM1", isin="INE000000001",
            quantity=Decimal("10"), average_price=Decimal("100.00"),
            last_price=Decimal("110.00"), exchange="NSE",
        )
    ]
    connector = StubBrokerConnector(holdings=holdings)
    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    portfolio.import_holdings(user_id, broker_id)

    after_success = _last_import_at(infra, user_id, broker_id)
    assert after_success is not None, "last_import_at should be set after successful import"

    # Failing import must NOT update last_import_at
    from components.c01_user_portfolio import BrokerAuthError as ImportedBrokerAuthError
    connector = StubBrokerConnector(holdings=[], raise_on=ImportedBrokerAuthError("token expired"))
    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    with pytest.raises(ImportedBrokerAuthError):
        portfolio.import_holdings(user_id, broker_id)

    after_failure = _last_import_at(infra, user_id, broker_id)
    assert after_failure == after_success, (
        f"last_import_at must not change on failure: was {after_success}, got {after_failure}"
    )

    # Cleanup
    infra._connection().execute(
        "DELETE FROM broker_holdings WHERE user_id = %s AND broker_id = %s",
        (user_id, broker_id),
    )
    infra._connection().execute(
        "DELETE FROM broker_connections WHERE user_id = %s AND broker_id = %s",
        (user_id, broker_id),
    )
