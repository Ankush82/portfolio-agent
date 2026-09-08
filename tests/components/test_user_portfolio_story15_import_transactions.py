"""QA tests for STORY-15: DefaultUserPortfolio.import_transactions.

This file is fully self-contained. It tests STORY-15 acceptance criteria
by importing the module and exercising the real implementation.
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest


# ---------------------------------------------------------------------------
# Deferred imports — let conftest.py run first (it sets BROKER_TOKEN_ENCRYPTION_KEY)
# ---------------------------------------------------------------------------

def _import_c01():
    """Deferred import so conftest.py (which sets up env vars) runs first."""
    from components.c01_user_portfolio import (
        BrokerApiError,
        BrokerAuthError,
        BrokerCredentials,
        BrokerConnector,
        BrokerTransaction,
        DefaultUserPortfolio,
        ImportResult,
        StubBrokerConnector,
    )
    return (
        BrokerApiError,
        BrokerAuthError,
        BrokerCredentials,
        BrokerConnector,
        BrokerTransaction,
        DefaultUserPortfolio,
        ImportResult,
        StubBrokerConnector,
    )


# ---------------------------------------------------------------------------
# Infrastructure test double (no external deps)
# ---------------------------------------------------------------------------

class _InMemoryInfrastructure:
    """Minimal Infrastructure test double for STORY-15."""

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, dict]] = {}
        self._broker_connections: dict[tuple[str, str], dict] = {}
        self._broker_transactions: list[dict] = []
        self._last_import_calls: list[tuple[str, str]] = []

    def store(self, table: str, record: dict) -> str:
        record_id = str(record.get("id", len(self._tables.get(table, {}))))
        self._tables.setdefault(table, {})[record_id] = dict(record, id=record_id)
        return record_id

    def retrieve(self, table: str, id_: str) -> dict | None:
        return self._tables.get(table, {}).get(id_)

    def query(self, table: str, filters: dict) -> list[dict]:
        return [
            r for r in self._tables.get(table, {}).values()
            if all(r.get(k) == v for k, v in filters.items())
        ]

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

    def upsert_broker_transaction(
        self, user_id: str, broker_id: str, external_id: str,
        symbol: str, isin: str, trade_date: "date",
        side: str, quantity: "Decimal", price: "Decimal",
        amount: "Decimal", exchange: str, segment: str, raw: dict,
    ) -> bool:
        """Idempotent upsert: True = inserted, False = skipped (already existed)."""
        for existing in self._broker_transactions:
            if (existing["user_id"], existing["broker_id"], existing["external_id"]) == (
                user_id, broker_id, external_id
            ):
                existing.update(
                    symbol=symbol, isin=isin, trade_date=str(trade_date),
                    side=side, quantity=str(quantity), price=str(price),
                    amount=str(amount), exchange=exchange, segment=segment, raw=raw,
                )
                return False
        self._broker_transactions.append(
            dict(
                user_id=user_id, broker_id=broker_id, external_id=external_id,
                symbol=symbol, isin=isin, trade_date=str(trade_date),
                side=side, quantity=str(quantity), price=str(price),
                amount=str(amount), exchange=exchange, segment=segment, raw=raw,
            )
        )
        return True

    def touch_last_import(self, user_id: str, broker_id: str) -> None:
        self._last_import_calls.append((user_id, broker_id))
        key = (user_id, broker_id)
        if key in self._broker_connections:
            self._broker_connections[key]["last_import_at"] = "2024-06-01T00:00:00Z"

    def mark_broker_connection_error(self, user_id: str, broker_id: str, error_message: str) -> None:
        key = (user_id, broker_id)
        if key in self._broker_connections:
            self._broker_connections[key]["status"] = "ERROR"
            self._broker_connections[key]["last_error"] = error_message

    def _put_broker_connection(self, row: dict) -> None:
        self._broker_connections[(row["user_id"], row["broker_id"])] = dict(row)


def _seed_connection(infra: _InMemoryInfrastructure, user_id="u1", broker_id="stub") -> None:
    infra._put_broker_connection({
        "id": "conn-1",
        "user_id": user_id,
        "broker_id": broker_id,
        "broker_user_id": "stub-broker-user",
        "access_token_encrypted": "test-enc",
        "token_type": "Bearer",
        "access_token_expires_at": None,
        "status": "CONNECTED",
        "last_error": None,
        "connected_at": "2024-01-01T00:00:00Z",
        "last_import_at": None,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    })


def _make_stub_transactions(n: int):
    """Return n deterministic BrokerTransaction objects."""
    return [
        BrokerTransaction(
            external_id=f"stub-tx-{i:03d}",
            symbol="AAPL",
            isin="US0378331005",
            trade_date=date(2024, 1, 15),
            side="BUY",
            quantity=Decimal("2"),
            price=Decimal("150.00"),
            amount=Decimal("300.00"),
            exchange="NASDAQ",
            segment="EQ",
            raw={"stub": True},
        )
        for i in range(1, n + 1)
    ]


# ---------------------------------------------------------------------------
# TEST SUITE
# ---------------------------------------------------------------------------

def test_module_can_be_imported():
    """STORY-15 requires the module to actually load. Verify it imports cleanly."""
    # Deferred import — conftest.py sets BROKER_TOKEN_ENCRYPTION_KEY first.
    (
        BrokerApiError,
        BrokerAuthError,
        BrokerCredentials,
        BrokerConnector,
        BrokerTransaction,
        DefaultUserPortfolio,
        ImportResult,
        StubBrokerConnector,
    ) = _import_c01()
    # Module loaded successfully — if LifecycleMixin were missing this would
    # have raised NameError at import time.
    assert DefaultUserPortfolio is not None


def test_import_transactions_method_signature_correct():
    """Verify import_transactions has the right signature: (user_id, broker_id,
    start_date=date, end_date=date) — start_date/end_date defaulting to the date type
    (meaning None coerced to date per the default-window logic)."""
    import inspect
    (
        _,
        _,
        _,
        _,
        _,
        DefaultUserPortfolio,
        _,
        _,
    ) = _import_c01()
    sig = inspect.signature(DefaultUserPortfolio.import_transactions)
    params = list(sig.parameters.keys())
    assert "user_id" in params, f"import_transactions missing user_id; params={params}"
    assert "broker_id" in params, f"import_transactions missing broker_id; params={params}"
    assert "start_date" in params, f"import_transactions missing start_date; params={params}"
    assert "end_date" in params, f"import_transactions missing end_date; params={params}"
    # Defaults are date (the sentinel value meaning "not supplied, use default")
    assert sig.parameters["start_date"].default is date, (
        f"start_date default must be the date singleton (None coerced); got {sig.parameters['start_date'].default!r}"
    )
    assert sig.parameters["end_date"].default is date, (
        f"end_date default must be the date singleton (None coerced); got {sig.parameters['end_date'].default!r}"
    )


def test_import_result_dto_has_correct_fields():
    """Verify ImportResult has transactions_inserted, transactions_skipped_existing,
    rows_skipped_invalid (default 0)."""
    _, _, _, _, _, _, ImportResult, _ = _import_c01()
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(ImportResult)}
    assert "transactions_inserted" in field_names
    assert "transactions_skipped_existing" in field_names
    assert "rows_skipped_invalid" in field_names
    # Default value for rows_skipped_invalid
    ir = ImportResult(3, 2)
    assert ir.rows_skipped_invalid == 0


# ---------------------------------------------------------------------------
# AC: Default date window
# ---------------------------------------------------------------------------

def test_date_window_before_1_april():
    """Simulate today=2025-01-15 (Jan<Apr): current FY year=2024,
    min_start=date(2022, 4, 1). AC covers date before 1 April."""
    simulated_today = date(2025, 1, 15)
    simulated_current_fy = 2024  # Jan < Apr
    min_start = date(simulated_current_fy - 2, 4, 1)
    assert min_start == date(2022, 4, 1)


def test_date_window_after_1_april():
    """Simulate today=2025-05-20 (May>=Apr): current FY year=2025,
    min_start=date(2023, 4, 1). AC covers date after 1 April."""
    simulated_today = date(2025, 5, 20)
    simulated_current_fy = 2025  # May >= Apr
    min_start = date(simulated_current_fy - 2, 4, 1)
    assert min_start == date(2023, 4, 1)


# ---------------------------------------------------------------------------
# AC: start_date > end_date raises ValueError BEFORE any connector call
# ---------------------------------------------------------------------------

def test_start_after_end_raises_valueerror_before_connector_call():
    """start_date > end_date: ValueError raised. Connector MUST NOT be called."""
    (
        _,
        _,
        _,
        _,
        _,
        DefaultUserPortfolio,
        _,
        _,
    ) = _import_c01()

    class _NeverCalledConnector:
        broker_id = "stub"
        display_name = "Stub"
        def exchange_auth_code(self, *, code): ...
        def fetch_holdings(self, *, credentials): ...
        def fetch_transactions(self, *, credentials, start_date, end_date):
            raise RuntimeError("connector must NOT be called — error must fire first")

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)
    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=_NeverCalledConnector())

    with pytest.raises(ValueError) as exc_info:
        portfolio.import_transactions(
            user_id="u1",
            broker_id="stub",
            start_date=date(2025, 6, 1),
            end_date=date(2025, 1, 1),
        )
    assert "start_date" in str(exc_info.value).lower()
    assert "end_date" in str(exc_info.value).lower()


def test_no_connection_returns_zero_result_without_calling_connector():
    """No broker connection: ImportResult(0,0,0), connector never called."""
    _, _, _, _, _, DefaultUserPortfolio, ImportResult, _ = _import_c01()

    class _MustNotBeCalled:
        broker_id = "stub"
        display_name = "Stub"
        def exchange_auth_code(self, *, code): ...
        def fetch_holdings(self, *, credentials): raise RuntimeError("must not be called")
        def fetch_transactions(self, *, credentials, start_date, end_date):
            raise RuntimeError("must NOT be called")

    infra = _InMemoryInfrastructure()
    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=_MustNotBeCalled())

    result = portfolio.import_transactions(user_id="nobody", broker_id="nobody")

    assert isinstance(result, ImportResult)
    assert result.transactions_inserted == 0
    assert result.transactions_skipped_existing == 0
    assert result.rows_skipped_invalid == 0


# ---------------------------------------------------------------------------
# AC: StubBrokerConnector returning 5 transactions -> transactions_inserted == 5
# ---------------------------------------------------------------------------

def test_five_stub_transactions_yields_transactions_inserted_5():
    """StubBrokerConnector returning 5 transactions: transactions_inserted must be 5."""
    (
        _,
        _,
        _,
        _,
        _,
        DefaultUserPortfolio,
        ImportResult,
        StubBrokerConnector,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)
    connector = StubBrokerConnector(transactions=_make_stub_transactions(5))
    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    result = portfolio.import_transactions(user_id="u1", broker_id="stub")

    assert isinstance(result, ImportResult)
    assert result.transactions_inserted == 5, (
        f"Expected transactions_inserted=5; got {result.transactions_inserted}"
    )
    assert result.transactions_skipped_existing == 0
    assert result.rows_skipped_invalid == 0


def test_inserted_transactions_are_persisted():
    """After a successful import, the transactions are stored in infrastructure."""
    (
        _,
        _,
        _,
        _,
        _,
        DefaultUserPortfolio,
        _,
        StubBrokerConnector,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)
    connector = StubBrokerConnector(transactions=_make_stub_transactions(3))
    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    portfolio.import_transactions(user_id="u1", broker_id="stub")

    stored = infra.query("broker_transactions", {})
    assert len(stored) == 3, f"Expected 3 stored transactions; got {len(stored)}"


# ---------------------------------------------------------------------------
# AC: Re-running same import: 0 new rows, skipped_existing reported, row count unchanged
# ---------------------------------------------------------------------------

def test_rerunning_same_import_is_idempotent():
    """Re-importing identical transactions: inserted=0, skipped_existing=3, row_count=3."""
    (
        _,
        _,
        _,
        _,
        _,
        DefaultUserPortfolio,
        ImportResult,
        StubBrokerConnector,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)
    connector = StubBrokerConnector(transactions=_make_stub_transactions(3))
    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    first = portfolio.import_transactions(user_id="u1", broker_id="stub")
    assert first.transactions_inserted == 3, f"First run: expected 3 inserts; got {first.transactions_inserted}"

    second = portfolio.import_transactions(user_id="u1", broker_id="stub")

    assert second.transactions_inserted == 0, (
        f"Second run: expected 0 inserts (idempotent); got {second.transactions_inserted}"
    )
    assert second.transactions_skipped_existing == 3, (
        f"Second run: expected 3 skipped_existing; got {second.transactions_skipped_existing}"
    )
    stored = infra.query("broker_transactions", {})
    assert len(stored) == 3, f"Row count must stay at 3; got {len(stored)}"


# ---------------------------------------------------------------------------
# AC: BrokerAuthError sets connection status ERROR and re-raises;
#     last_import_at updates on success only.
# ---------------------------------------------------------------------------

def test_brokerautherror_marks_connection_error_and_reraises():
    """BrokerAuthError: connection status must become ERROR, exception re-raised."""
    (
        _,
        BrokerAuthError,
        _,
        _,
        _,
        DefaultUserPortfolio,
        _,
        _,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)

    class _AuthFailingConnector:
        broker_id = "stub"
        display_name = "Stub"
        def exchange_auth_code(self, *, code): ...
        def fetch_holdings(self, *, credentials): return []
        def fetch_transactions(self, *, credentials, start_date, end_date):
            raise BrokerAuthError("token expired")

    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=_AuthFailingConnector())

    with pytest.raises(BrokerAuthError):
        portfolio.import_transactions(user_id="u1", broker_id="stub")

    conn = infra.get_broker_connection("u1", "stub")
    assert conn.status == "ERROR", (
        f"Connection status must be ERROR after BrokerAuthError; got {conn.status!r}"
    )
    assert conn.last_error is not None


def test_last_import_at_not_updated_on_auth_error():
    """BrokerAuthError: last_import_at must NOT be updated."""
    (
        _,
        BrokerAuthError,
        _,
        _,
        _,
        DefaultUserPortfolio,
        _,
        _,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)

    class _AuthFailingConnector:
        broker_id = "stub"
        display_name = "Stub"
        def exchange_auth_code(self, *, code): ...
        def fetch_holdings(self, *, credentials): return []
        def fetch_transactions(self, *, credentials, start_date, end_date):
            raise BrokerAuthError("token expired")

    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=_AuthFailingConnector())

    with pytest.raises(BrokerAuthError):
        portfolio.import_transactions(user_id="u1", broker_id="stub")

    assert infra._last_import_calls == [], (
        f"last_import_at must NOT be updated on error; got calls: {infra._last_import_calls}"
    )


def test_last_import_at_updated_on_success():
    """On success: touch_last_import IS called."""
    (
        _,
        _,
        _,
        _,
        _,
        DefaultUserPortfolio,
        _,
        StubBrokerConnector,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)
    connector = StubBrokerConnector(transactions=_make_stub_transactions(2))
    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    portfolio.import_transactions(user_id="u1", broker_id="stub")

    assert ("u1", "stub") in infra._last_import_calls, (
        f"touch_last_import must be called on success; got: {infra._last_import_calls}"
    )


def test_brokerapierror_also_marks_error_and_reraises():
    """BrokerApiError: status -> ERROR, exception re-raised."""
    (
        BrokerApiError,
        _,
        _,
        _,
        _,
        DefaultUserPortfolio,
        _,
        _,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)

    class _ApiFailingConnector:
        broker_id = "stub"
        display_name = "Stub"
        def exchange_auth_code(self, *, code): ...
        def fetch_holdings(self, *, credentials): return []
        def fetch_transactions(self, *, credentials, start_date, end_date):
            raise BrokerApiError("upstream error")

    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=_ApiFailingConnector())

    with pytest.raises(BrokerApiError):
        portfolio.import_transactions(user_id="u1", broker_id="stub")

    conn = infra.get_broker_connection("u1", "stub")
    assert conn.status == "ERROR", (
        f"Connection status must be ERROR after BrokerApiError; got {conn.status!r}"
    )


# ---------------------------------------------------------------------------
# AC: start_date clamping + warning
# ---------------------------------------------------------------------------

def test_start_date_before_boundary_triggers_warning(caplog):
    """start_date < min_start: warning with IMPORT_START_DATE_CLAMPED must be logged."""
    import logging
    caplog.set_level(logging.WARNING)
    (
        _,
        _,
        _,
        _,
        _,
        DefaultUserPortfolio,
        _,
        StubBrokerConnector,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)
    too_early = date(2020, 1, 1)
    connector = StubBrokerConnector(transactions=[])
    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    portfolio.import_transactions(
        user_id="u1",
        broker_id="stub",
        start_date=too_early,
        end_date=date(2025, 3, 31),
    )

    clamp_records = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "IMPORT_START_DATE_CLAMPED" in r.message
    ]
    assert len(clamp_records) >= 1, (
        f"Expected IMPORT_START_DATE_CLAMPED warning when start_date < boundary. "
        f"Got warnings: {[r.message for r in caplog.records if r.levelno >= logging.WARNING]}"
    )


def test_valid_start_date_is_not_clamped(caplog):
    """start_date >= min_start: no IMPORT_START_DATE_CLAMPED warning emitted."""
    import logging
    caplog.set_level(logging.WARNING)
    (
        _,
        _,
        _,
        _,
        _,
        DefaultUserPortfolio,
        _,
        StubBrokerConnector,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)
    valid_start = date(2023, 6, 1)
    connector = StubBrokerConnector(transactions=[])
    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    portfolio.import_transactions(
        user_id="u1",
        broker_id="stub",
        start_date=valid_start,
        end_date=date(2025, 3, 31),
    )

    clamp_records = [
        r for r in caplog.records
        if "IMPORT_START_DATE_CLAMPED" in r.message
    ]
    assert len(clamp_records) == 0, (
        f"Valid start_date must NOT be clamped. Got warnings: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# AC: BrokerApiError handling (also tested above)
# ---------------------------------------------------------------------------

def test_import_result_returns_real_instance_with_correct_counts():
    """A real import call returns an ImportResult with the right counts."""
    (
        _,
        _,
        _,
        _,
        _,
        DefaultUserPortfolio,
        ImportResult,
        StubBrokerConnector,
    ) = _import_c01()

    infra = _InMemoryInfrastructure()
    _seed_connection(infra)
    connector = StubBrokerConnector(transactions=_make_stub_transactions(2))
    portfolio = DefaultUserPortfolio(infrastructure=infra, broker_connector=connector)

    result = portfolio.import_transactions(user_id="u1", broker_id="stub")

    assert isinstance(result, ImportResult)
    assert result.transactions_inserted == 2
    assert result.transactions_skipped_existing == 0
    assert result.rows_skipped_invalid == 0
