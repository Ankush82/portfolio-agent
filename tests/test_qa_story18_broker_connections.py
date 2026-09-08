"""QA tests for STORY-18 — GET /api/brokers/connections status endpoint.

Flask test client + real Postgres only; no network calls.
"""

from __future__ import annotations

import unittest.mock
from datetime import datetime, timezone

import pytest

from webapp import create_app
from components.c01_user_portfolio import (
    BrokerCredentials,
    StubBrokerConnector,
    register_broker_connector,
    unregister_broker_connector,
)
from infrastructure_postgres import DefaultInfrastructure


def _seed_user(user_id: str) -> None:
    """Insert a minimal `users` row so broker_connections' real FK
    constraint (user_id references users(id)) is satisfiable -- this
    table has no other required columns."""
    infra = DefaultInfrastructure()
    with infra._connection().cursor() as cursor:
        cursor.execute(
            "INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
            (user_id,),
        )


def _make_authenticated_client(app, user_id):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    return client


def _register_real_upstox_connector(app):
    with app.app_context():
        with unittest.mock.patch.dict("os.environ", {
            "UPSTOX_CLIENT_ID": "test-client-id",
            "UPSTOX_CLIENT_SECRET": "test-client-secret",
            "UPSTOX_REDIRECT_URI": "https://example.com/callback",
        }):
            from upstox_config import UpstoxConfig
            config = UpstoxConfig.from_env()
            connector = type(
                "TestUpstoxConnector",
                (),
                {
                    "broker_id": "upstox",
                    "display_name": "Upstox",
                    "_config": config,
                    "build_authorize_url": lambda self, state: "https://example.com/authorize",
                },
            )()
        with app.app_context():
            register_broker_connector(connector)
        return connector


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for broker_id in ("upstox", "fake-broker"):
        unregister_broker_connector(broker_id)


def test_unauthenticated_returns_401():
    app = create_app()
    client = app.test_client()
    response = client.get("/api/brokers/connections")
    assert response.status_code == 401


def test_no_connections_returns_empty_list_and_nonempty_available_brokers():
    app = create_app()
    register_broker_connector(StubBrokerConnector())
    client = _make_authenticated_client(app, "story18-user-empty")

    response = client.get("/api/brokers/connections")

    assert response.status_code == 200
    data = response.get_json()
    assert data["connections"] == []
    assert len(data["available_brokers"]) > 0


def test_connected_upstox_row_exposes_status_and_timestamps():
    app = create_app()
    with app.app_context():
        register_broker_connector(type("TestUpstox", (), {"broker_id": "upstox", "display_name": "Upstox"})())
    user_id = "story18-user-connected"
    _seed_user(user_id)

    infra = DefaultInfrastructure()
    connected_at = datetime.now(timezone.utc)
    infra.upsert_broker_connection(
        user_id=user_id,
        broker_id="upstox",
        credentials=BrokerCredentials(access_token="tok", token_type="Bearer"),
        status="CONNECTED",
        connected_at=connected_at,
    )

    client = _make_authenticated_client(app, user_id)
    response = client.get("/api/brokers/connections")
    data = response.get_json()

    conn = next(c for c in data["connections"] if c["broker_id"] == "upstox")
    assert conn["status"] == "CONNECTED"
    assert conn["connected_at"] is not None
    assert "last_import_at" in conn
    assert "broker_user_id" in conn


def test_available_brokers_reflects_registry_not_a_hardcoded_list():
    app = create_app()
    fake_connector = type(
        "FakeBroker", (), {"broker_id": "fake-broker", "display_name": "Fake Broker"}
    )()
    with app.app_context():
        register_broker_connector(fake_connector)

    client = _make_authenticated_client(app, "story18-user-registry")
    response = client.get("/api/brokers/connections")
    data = response.get_json()

    broker_ids = {b["broker_id"] for b in data["available_brokers"]}
    assert "fake-broker" in broker_ids


def test_configured_is_false_when_upstox_env_vars_unset():
    app = create_app()
    with app.app_context():
        connector = type(
            "TestUpstoxConnector", (), {"broker_id": "upstox", "display_name": "Upstox"}
        )()
        register_broker_connector(connector)

    with unittest.mock.patch.dict("os.environ", {}, clear=False):
        for key in ("UPSTOX_CLIENT_ID", "UPSTOX_CLIENT_SECRET", "UPSTOX_REDIRECT_URI"):
            __import__("os").environ.pop(key, None)

        client = _make_authenticated_client(app, "story18-user-unconfigured")
        response = client.get("/api/brokers/connections")

    assert response.status_code == 200
    data = response.get_json()
    upstox = next(b for b in data["available_brokers"] if b["broker_id"] == "upstox")
    assert upstox["configured"] is False


def test_error_connection_exposes_last_error():
    app = create_app()
    with app.app_context():
        register_broker_connector(type("TestUpstox", (), {"broker_id": "upstox", "display_name": "Upstox"})())
    user_id = "story18-user-error"
    _seed_user(user_id)

    infra = DefaultInfrastructure()
    infra.upsert_broker_connection(
        user_id=user_id,
        broker_id="upstox",
        credentials=BrokerCredentials(access_token="tok", token_type="Bearer"),
        status="CONNECTED",
    )
    infra.mark_broker_connection_error(user_id, "upstox", "Upstox access expired, please reconnect")

    client = _make_authenticated_client(app, user_id)
    response = client.get("/api/brokers/connections")
    data = response.get_json()

    conn = next(c for c in data["connections"] if c["broker_id"] == "upstox")
    assert conn["status"] == "ERROR"
    assert conn["last_error"] == "Upstox access expired, please reconnect"


def test_no_access_token_in_response():
    app = create_app()
    register_broker_connector(StubBrokerConnector())
    user_id = "story18-user-token-check"
    _seed_user(user_id)

    infra = DefaultInfrastructure()
    infra.upsert_broker_connection(
        user_id=user_id,
        broker_id="upstox",
        credentials=BrokerCredentials(access_token="super-secret-token", token_type="Bearer"),
        status="CONNECTED",
    )

    client = _make_authenticated_client(app, user_id)
    response = client.get("/api/brokers/connections")
    body_text = response.get_data(as_text=True)

    assert "super-secret-token" not in body_text
    assert "access_token" not in body_text
    assert "access_token_encrypted" not in body_text


def test_one_user_cannot_see_another_users_connections():
    app = create_app()
    register_broker_connector(StubBrokerConnector())

    infra = DefaultInfrastructure()
    _seed_user("story18-user-a")
    infra.upsert_broker_connection(
        user_id="story18-user-a",
        broker_id="upstox",
        credentials=BrokerCredentials(access_token="tok-a", token_type="Bearer"),
        status="CONNECTED",
    )

    client_b = _make_authenticated_client(app, "story18-user-b")
    response = client_b.get("/api/brokers/connections")
    data = response.get_json()

    assert data["connections"] == []
