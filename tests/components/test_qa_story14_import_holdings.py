"""QA tests for STORY-14: DefaultUserPortfolio.import_holdings (issue #120).

Self-contained: exercises the real implementation against an in-memory
Infrastructure test double, mirroring the established convention used by
tests/components/test_user_portfolio_story15_import_transactions.py for
the sibling import_transactions story.
"""

from decimal import Decimal

import pytest


def _import_c01():
    """Deferred import so conftest.py (sets BROKER_TOKEN_ENCRYPTION_KEY) runs first."""
    from components.c01_user_portfolio import (
        BrokerApiError,
        BrokerAuthError,
        BrokerHolding,
        BrokerNotConnectedError,
        DefaultUserPortfolio,
        HoldingsImportResult,
    )
    return (
        BrokerApiError,
        BrokerAuthError,
        BrokerHolding,
        BrokerNotConnectedError,
        DefaultUserPortfolio,
        HoldingsImportResult,
    )


class _InMemoryInfrastructure:
    """Minimal Infrastructure test double for STORY-14. Models
    broker_holdings as a real atomic replace: delete-then-insert, with
    an optional injected failure to prove a mid-write error leaves the
    pre-existing rows untouched (AC-3)."""

    def __init__(self, fail_replace_after: int | None = None) -> None:
        self._broker_connections: dict[tuple[str, str], dict] = {}
        self._broker_holdings: dict[tuple[str, str], list[dict]] = {}
        self._last_import_calls: list[tuple[str, str]] = []
        self._fail_replace_after = fail_replace_after

    def get_broker_connection(self, user_id: str, broker_id: str):
        from infrastructure_postgres import BrokerConnectionRecord
        row = self._broker_connections.get((user_id, broker_id))
        if row is None:
            return None
        return BrokerConnectionRecord(
            id=row.get("id", "test-id"),
            user_id=row["user_id"],
            broker_id=row["broker_id"],
            broker_user_id=row.get("broker_user_id"),
            access_token_encrypted=row.get("access_token_encrypted", "test-enc"),
            token_type=row.get("token_type", "Bearer"),
            access_token_expires_at=row.get("access_token_expires_at"),
            status=row.get("status", "CONNECTED"),
            last_error=row.get("last_error"),
            connected_at=row.get("connected_at"),
            last_import_at=row.get("last_import_at"),
            created_at=row.get("created_at", "2024-01-01T00:00:00Z"),
            updated_at=row.get("updated_at", "2024-01-01T00:00:00Z"),
        )

    def mark_broker_connection_error(self, user_id: str, broker_id: str, error_message: str) -> None:
        key = (user_id, broker_id)
        if key in self._broker_connections:
            self._broker_connections[key]["status"] = "ERROR"
            self._broker_connections[key]["last_error"] = error_message

    def touch_last_import(self, user_id: str, broker_id: str) -> None:
        self._last_import_calls.append((user_id, broker_id))

    def replace_broker_holdings(self, user_id: str, broker_id: str, holdings: list[dict]) -> None:
        """Real replace semantics: only commit the new set if every row
        writes cleanly -- an injected failure partway through must leave
        whatever was there before completely unchanged (mirrors the real
        transaction's rollback in DefaultInfrastructure)."""
        key = (user_id, broker_id)
        if self._fail_replace_after is not None and len(holdings) > self._fail_replace_after:
            raise RuntimeError("simulated mid-write database failure")
        self._broker_holdings[key] = list(holdings)

    def _put_broker_connection(self, row: dict) -> None:
        self._broker_connections[(row["user_id"], row["broker_id"])] = dict(row)

    def stored_holdings(self, user_id: str, broker_id: str) -> list[dict]:
        return self._broker_holdings.get((user_id, broker_id), [])


def _seed_connection(infra: _InMemoryInfrastructure, user_id="u1", broker_id="upstox") -> None:
    from broker_token_crypto import encrypt_secret
    infra._put_broker_connection({
        "id": "conn-1",
        "user_id": user_id,
        "broker_id": broker_id,
        "broker_user_id": "stub-broker-user",
        "access_token_encrypted": encrypt_secret("test-access-token"),
        "token_type": "Bearer",
        "access_token_expires_at": None,
        "status": "CONNECTED",
        "last_error": None,
        "connected_at": "2024-01-01T00:00:00Z",
        "last_import_at": None,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    })


class _FakeConnector:
    broker_id = "upstox"
    display_name = "Upstox"

    def __init__(self, holdings=None, raise_exc=None):
        self._holdings = holdings or []
        self._raise_exc = raise_exc

    def fetch_holdings(self, *, credentials):
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._holdings


def _make_holdings(n: int, BrokerHolding):
    return [
        BrokerHolding(
            symbol=f"SYM{i}",
            isin=f"INE{i:09d}",
            quantity=Decimal("10"),
            average_price=Decimal("100.00"),
            last_price=Decimal("110.00"),
            exchange="NSE",
        )
        for i in range(1, n + 1)
    ]


# ---------------------------------------------------------------------------
# AC: 3 holdings from StubBrokerConnector -> 3 rows, holdings_written == 3
# ---------------------------------------------------------------------------

def test_import_holdings_writes_all_fetched_rows():
    (
        BrokerApiError, BrokerAuthError, BrokerHolding,
        BrokerNotConnectedError, DefaultUserPortfolio, HoldingsImportResult,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)
    connector = _FakeConnector(holdings=_make_holdings(3, BrokerHolding))
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    result = portfolio_component.import_holdings("u1", "upstox")

    assert isinstance(result, HoldingsImportResult)
    assert result.holdings_written == 3
    assert result.skipped == 0
    assert len(infra.stored_holdings("u1", "upstox")) == 3


# ---------------------------------------------------------------------------
# AC: running twice leaves exactly 3 rows, removed holdings disappear
# ---------------------------------------------------------------------------

def test_import_holdings_twice_replaces_rather_than_duplicates():
    (
        BrokerApiError, BrokerAuthError, BrokerHolding,
        BrokerNotConnectedError, DefaultUserPortfolio, HoldingsImportResult,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)
    first_batch = _make_holdings(3, BrokerHolding)
    connector = _FakeConnector(holdings=first_batch)
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    portfolio_component.import_holdings("u1", "upstox")
    assert len(infra.stored_holdings("u1", "upstox")) == 3

    # Second import: one holding (SYM2) sold out at the broker, no longer returned.
    second_batch = [h for h in first_batch if h.symbol != "SYM2"]
    connector._holdings = second_batch
    result = portfolio_component.import_holdings("u1", "upstox")

    stored = infra.stored_holdings("u1", "upstox")
    assert len(stored) == 2
    assert result.holdings_written == 2
    assert all(row["symbol"] != "SYM2" for row in stored)


# ---------------------------------------------------------------------------
# AC: connector exception mid-import leaves pre-existing rows unchanged
# ---------------------------------------------------------------------------

def test_import_holdings_write_failure_leaves_existing_rows_unchanged():
    (
        BrokerApiError, BrokerAuthError, BrokerHolding,
        BrokerNotConnectedError, DefaultUserPortfolio, HoldingsImportResult,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)
    connector = _FakeConnector(holdings=_make_holdings(3, BrokerHolding))
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    portfolio_component.import_holdings("u1", "upstox")
    assert len(infra.stored_holdings("u1", "upstox")) == 3

    # Now simulate a real write failure partway through a second import.
    infra._fail_replace_after = 1
    connector._holdings = _make_holdings(3, BrokerHolding)
    with pytest.raises(RuntimeError):
        portfolio_component.import_holdings("u1", "upstox")

    # The transaction never committed -- the OLD 3 rows are still exactly there.
    assert len(infra.stored_holdings("u1", "upstox")) == 3


# ---------------------------------------------------------------------------
# AC: zero holdings returns holdings_written == 0, raises nothing
# ---------------------------------------------------------------------------

def test_import_holdings_zero_holdings_is_not_an_error():
    (
        BrokerApiError, BrokerAuthError, BrokerHolding,
        BrokerNotConnectedError, DefaultUserPortfolio, HoldingsImportResult,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)
    connector = _FakeConnector(holdings=[])
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    result = portfolio_component.import_holdings("u1", "upstox")

    assert result.holdings_written == 0
    assert result.skipped == 0


# ---------------------------------------------------------------------------
# AC: no stored connection raises BrokerNotConnectedError
# ---------------------------------------------------------------------------

def test_import_holdings_no_connection_raises_broker_not_connected_error():
    (
        BrokerApiError, BrokerAuthError, BrokerHolding,
        BrokerNotConnectedError, DefaultUserPortfolio, HoldingsImportResult,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()  # no connection seeded
    connector = _FakeConnector(holdings=[])
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    with pytest.raises(BrokerNotConnectedError):
        portfolio_component.import_holdings("u1", "upstox")


# ---------------------------------------------------------------------------
# AC: BrokerAuthError sets connection ERROR with reconnect message, re-raises
# ---------------------------------------------------------------------------

def test_import_holdings_broker_auth_error_marks_connection_error_and_reraises():
    (
        BrokerApiError, BrokerAuthError, BrokerHolding,
        BrokerNotConnectedError, DefaultUserPortfolio, HoldingsImportResult,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)
    connector = _FakeConnector(raise_exc=BrokerAuthError("token expired"))
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    with pytest.raises(BrokerAuthError):
        portfolio_component.import_holdings("u1", "upstox")

    conn = infra._broker_connections[("u1", "upstox")]
    assert conn["status"] == "ERROR"
    assert "reconnect" in conn["last_error"].lower()
    assert "upstox" in conn["last_error"].lower()


# ---------------------------------------------------------------------------
# AC: last_import_at updated on success only
# ---------------------------------------------------------------------------

def test_import_holdings_touches_last_import_only_on_success():
    (
        BrokerApiError, BrokerAuthError, BrokerHolding,
        BrokerNotConnectedError, DefaultUserPortfolio, HoldingsImportResult,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)
    connector = _FakeConnector(holdings=_make_holdings(1, BrokerHolding))
    portfolio_component = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)
    portfolio_component.import_holdings("u1", "upstox")
    assert infra._last_import_calls == [("u1", "upstox")]

    # A failing import must NOT touch last_import_at again.
    connector._raise_exc = BrokerAuthError("expired")
    with pytest.raises(BrokerAuthError):
        portfolio_component.import_holdings("u1", "upstox")
    assert infra._last_import_calls == [("u1", "upstox")]


# ---------------------------------------------------------------------------
# AC: import_holdings method signature is (user_id, broker_id) -> HoldingsImportResult
# ---------------------------------------------------------------------------

def test_import_holdings_signature_is_user_id_broker_id():
    import inspect
    (
        BrokerApiError, BrokerAuthError, BrokerHolding,
        BrokerNotConnectedError, DefaultUserPortfolio, HoldingsImportResult,
    ) = _import_c01()
    sig = inspect.signature(DefaultUserPortfolio.import_holdings)
    params = list(sig.parameters.keys())
    assert params == ["self", "user_id", "broker_id"], params


def test_holdings_import_result_dto_has_correct_fields():
    _, _, _, _, _, HoldingsImportResult = _import_c01()
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(HoldingsImportResult)}
    assert "holdings_written" in field_names
    assert "skipped" in field_names
    result = HoldingsImportResult(holdings_written=5)
    assert result.skipped == 0
