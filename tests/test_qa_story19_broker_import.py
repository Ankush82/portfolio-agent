"""QA tests for STORY-19 — POST /api/brokers/{broker_id}/import.

Flask test client + real Postgres only; no network calls.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from webapp import create_app
from components.c01_user_portfolio import (
    BrokerApiError,
    BrokerAuthError,
    BrokerConfigError,
    BrokerCredentials,
    BrokerRateLimitError,
    BrokerTransaction,
    StubBrokerConnector,
    register_broker_connector,
    unregister_broker_connector,
)
from infrastructure_postgres import DefaultInfrastructure


def _recent_transactions(n: int = 4) -> list[BrokerTransaction]:
    """Real transactions dated within the last few days -- the stub's own
    default transactions are fixed at 2024 dates, which fall outside
    import_transactions' real 3-financial-year window relative to
    whatever "today" actually is when these tests run."""
    today = date.today()
    return [
        BrokerTransaction(
            external_id=f"story19-tx-{i:03d}",
            symbol="AAPL",
            isin="US0378331005",
            trade_date=today - timedelta(days=i),
            side="BUY",
            quantity=Decimal("1"),
            price=Decimal("150.00"),
            amount=Decimal("150.00"),
            exchange="NASDAQ",
            segment="EQ",
            raw={"story19": True},
        )
        for i in range(n)
    ]


def _seed_user(user_id: str) -> None:
    infra = DefaultInfrastructure()
    with infra._connection().cursor() as cursor:
        cursor.execute(
            "INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
            (user_id,),
        )


def _seed_connection(user_id: str, broker_id: str = "stub") -> None:
    _seed_user(user_id)
    infra = DefaultInfrastructure()
    infra.upsert_broker_connection(
        user_id=user_id,
        broker_id=broker_id,
        credentials=BrokerCredentials(access_token="tok", token_type="Bearer"),
        status="CONNECTED",
    )


def _make_authenticated_client(app, user_id):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    return client


@pytest.fixture(autouse=True)
def _clean_registry():
    infra = DefaultInfrastructure()
    with infra._connection().cursor() as cursor:
        cursor.execute("DELETE FROM broker_transactions WHERE user_id LIKE 'story19-%'")
        cursor.execute("DELETE FROM broker_holdings WHERE user_id LIKE 'story19-%'")
    yield
    unregister_broker_connector("stub")


def test_unauthenticated_returns_401():
    app = create_app()
    client = app.test_client()
    response = client.post("/api/brokers/stub/import")
    assert response.status_code == 401


def test_unknown_broker_id_returns_404():
    app = create_app()
    client = _make_authenticated_client(app, "story19-user-unknown-broker")
    response = client.post("/api/brokers/definitely-not-registered/import")
    assert response.status_code == 404


def test_happy_path_returns_real_counts_and_fresh_last_import_at():
    app = create_app()
    register_broker_connector(StubBrokerConnector(transactions=_recent_transactions(4)))
    user_id = "story19-user-happy"
    _seed_connection(user_id)

    client = _make_authenticated_client(app, user_id)
    response = client.post("/api/brokers/stub/import")

    assert response.status_code == 200
    data = response.get_json()
    assert data["broker_id"] == "stub"
    assert data["holdings_written"] == 2
    assert data["transactions_inserted"] == 4
    assert data["transactions_skipped_existing"] == 0
    assert data["rows_skipped_invalid"] == 0
    assert data["last_import_at"] is not None


def test_calling_twice_skips_existing_transactions_without_duplicating():
    app = create_app()
    register_broker_connector(StubBrokerConnector(transactions=_recent_transactions(4)))
    user_id = "story19-user-twice"
    _seed_connection(user_id)
    client = _make_authenticated_client(app, user_id)

    first = client.post("/api/brokers/stub/import")
    assert first.get_json()["transactions_inserted"] == 4

    second = client.post("/api/brokers/stub/import")
    second_data = second.get_json()
    assert second_data["transactions_inserted"] == 0
    assert second_data["transactions_skipped_existing"] == 4


def test_malformed_start_date_returns_400_without_calling_connector():
    app = create_app()
    connector = StubBrokerConnector()
    register_broker_connector(connector)
    user_id = "story19-user-bad-date"
    _seed_connection(user_id)
    client = _make_authenticated_client(app, user_id)

    response = client.post("/api/brokers/stub/import", json={"start_date": "not-a-date"})

    assert response.status_code == 400
    # No connection status change would happen from a real call -- the
    # cheapest real proxy: the connection is still exactly as seeded.
    infra = DefaultInfrastructure()
    conn = infra.get_broker_connection(user_id, "stub")
    assert conn.last_import_at is None


def test_malformed_end_date_returns_400():
    app = create_app()
    register_broker_connector(StubBrokerConnector())
    user_id = "story19-user-bad-end-date"
    _seed_connection(user_id)
    client = _make_authenticated_client(app, user_id)

    response = client.post("/api/brokers/stub/import", json={"end_date": "13/45/2024"})

    assert response.status_code == 400


def test_no_connection_returns_409_not_connected():
    app = create_app()
    register_broker_connector(StubBrokerConnector())
    user_id = "story19-user-no-connection"
    _seed_user(user_id)  # user exists, but never connected the broker

    client = _make_authenticated_client(app, user_id)
    response = client.post("/api/brokers/stub/import")

    assert response.status_code == 409
    assert response.get_json()["error"] == "not_connected"


def test_broker_auth_error_returns_401_reconnect_required():
    app = create_app()
    register_broker_connector(StubBrokerConnector(raise_on=BrokerAuthError("token expired")))
    user_id = "story19-user-auth-error"
    _seed_connection(user_id)

    client = _make_authenticated_client(app, user_id)
    response = client.post("/api/brokers/stub/import")

    assert response.status_code == 401
    data = response.get_json()
    assert data["error"] == "reconnect_required"
    assert "reconnect" in data["message"].lower()


def test_broker_rate_limit_error_returns_429():
    app = create_app()
    register_broker_connector(StubBrokerConnector(raise_on=BrokerRateLimitError("slow down")))
    user_id = "story19-user-rate-limit"
    _seed_connection(user_id)

    client = _make_authenticated_client(app, user_id)
    response = client.post("/api/brokers/stub/import")

    assert response.status_code == 429


def test_broker_api_error_returns_502():
    app = create_app()
    register_broker_connector(StubBrokerConnector(raise_on=BrokerApiError("broker returned 500")))
    user_id = "story19-user-api-error"
    _seed_connection(user_id)

    client = _make_authenticated_client(app, user_id)
    response = client.post("/api/brokers/stub/import")

    assert response.status_code == 502
    assert response.get_json()["error"] == "broker_api_error"


def test_broker_config_error_returns_503():
    app = create_app()
    register_broker_connector(StubBrokerConnector(raise_on=BrokerConfigError("missing credentials")))
    user_id = "story19-user-config-error"
    _seed_connection(user_id)

    client = _make_authenticated_client(app, user_id)
    response = client.post("/api/brokers/stub/import")

    assert response.status_code == 503
    assert response.get_json()["error"] == "broker_not_configured"


def test_user_cannot_trigger_import_for_another_users_connection():
    app = create_app()
    register_broker_connector(StubBrokerConnector())
    _seed_connection("story19-user-owner")
    _seed_user("story19-user-other")

    client_other = _make_authenticated_client(app, "story19-user-other")
    response = client_other.post("/api/brokers/stub/import")

    # The other user has no connection of their own -- not_connected,
    # never the owner's real data.
    assert response.status_code == 409
    assert response.get_json()["error"] == "not_connected"


def test_no_access_token_in_response():
    app = create_app()
    register_broker_connector(StubBrokerConnector())
    user_id = "story19-user-token-check"
    _seed_connection(user_id)

    client = _make_authenticated_client(app, user_id)
    response = client.post(
        "/api/brokers/stub/import",
        json={"start_date": "2024-01-01", "end_date": "2024-12-31"},
    )
    body_text = response.get_data(as_text=True)

    assert "stub-access-token" not in body_text
    assert "access_token" not in body_text
