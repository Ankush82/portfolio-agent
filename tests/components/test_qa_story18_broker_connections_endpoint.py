"""Fresh QA tests for STORY-18 — GET /api/brokers/connections status endpoint.

These tests are an independent re-verification of the ACs the story's
own endpoint claims to satisfy. They exercise the route through the
real Flask test client (no network) against the real Postgres that
``DefaultInfrastructure`` writes to, and assert on the real JSON
response shape — not on a parallel set of hardcoded values.

ACs verified:
  1. CONNECTED row exposes status, connected_at, last_import_at, broker_user_id.
  2. No connections -> connections == [] and available_brokers is non-empty.
  3. available_brokers is registry-driven (a freshly-registered fake broker appears).
  4. configured == False for upstox when UPSTOX_* env vars are unset; 200 still returned.
  5. ERROR row exposes last_error.
  6. No access-token-shaped string in the response; 401 when unauthenticated;
     one user cannot see another's connections.
"""

from __future__ import annotations

import os
import unittest.mock
import uuid
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


def _unique_user_id(prefix: str) -> str:
    """Unique user id per test so tests don't collide on the
    ``UNIQUE(user_id, broker_id)`` constraint on broker_connections."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _ensure_user_row(user_id: str) -> None:
    """Insert a minimal users row so broker_connections.user_id FK is satisfiable."""
    infra = DefaultInfrastructure()
    with infra._connection().cursor() as cursor:
        cursor.execute(
            "INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
            (user_id,),
        )


def _authenticated_client(app, user_id: str):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    return client


@pytest.fixture(autouse=True)
def _clean_registry_each_test():
    """Make sure every test starts with a clean broker_connector registry."""
    yield
    for broker_id in ("upstox", "ephemeral-fake-broker"):
        unregister_broker_connector(broker_id)


# ---------------------------------------------------------------------------
# AC #1 — CONNECTED row exposes status, connected_at, last_import_at, broker_user_id
# ---------------------------------------------------------------------------
def test_ac1_connected_row_exposes_required_fields_with_real_values():
    """A user with a real CONNECTED upstox row sees status=='CONNECTED',
    a real (non-None) connected_at, a last_import_at key (None is fine for
    a fresh row), and the broker_user_id they originally stored."""
    app = create_app()
    with app.app_context():
        # Use the real StubBrokerConnector (does NOT define upstox), so the
        # only registered broker will be 'stub'; we still need upstox in the
        # registry for the response to look up display_name. Register a tiny
        # upstox-shaped stub for that.
        register_broker_connector(
            type("UpstoxShim", (), {"broker_id": "upstox", "display_name": "Upstox"})()
        )

    user_id = _unique_user_id("ac1-connected")
    _ensure_user_row(user_id)

    real_connected_at = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    infra = DefaultInfrastructure()
    infra.upsert_broker_connection(
        user_id=user_id,
        broker_id="upstox",
        credentials=BrokerCredentials(
            access_token="real-secret-do-not-leak",
            token_type="Bearer",
            broker_user_id="real-upstox-user-99",
        ),
        status="CONNECTED",
        connected_at=real_connected_at,
    )

    client = _authenticated_client(app, user_id)
    response = client.get("/api/brokers/connections")

    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert "connections" in body and "available_brokers" in body

    upstox_conns = [c for c in body["connections"] if c["broker_id"] == "upstox"]
    assert len(upstox_conns) == 1, f"expected exactly one upstox connection, got {upstox_conns}"
    conn = upstox_conns[0]
    # status
    assert conn["status"] == "CONNECTED"
    # connected_at — non-null ISO8601 string
    assert conn["connected_at"] is not None, "connected_at must be present"
    # last_import_at key present (None is acceptable for a fresh row)
    assert "last_import_at" in conn
    # broker_user_id is the one we stored
    assert conn["broker_user_id"] == "real-upstox-user-99"
    # display_name comes from the registry, not the DB row
    assert conn["display_name"] == "Upstox"


# ---------------------------------------------------------------------------
# AC #2 — No connections -> connections == [] AND available_brokers non-empty
# ---------------------------------------------------------------------------
def test_ac2_no_connections_yields_empty_list_with_nonempty_available():
    app = create_app()
    # Register StubBrokerConnector so available_brokers has at least one entry.
    register_broker_connector(StubBrokerConnector())

    user_id = _unique_user_id("ac2-empty")
    # NB: do NOT seed any broker_connections row for this user.

    client = _authenticated_client(app, user_id)
    response = client.get("/api/brokers/connections")

    assert response.status_code == 200
    body = response.get_json()
    assert body["connections"] == [], f"expected empty list, got {body['connections']}"
    assert isinstance(body["available_brokers"], list)
    assert len(body["available_brokers"]) > 0, "available_brokers must not be empty"


# ---------------------------------------------------------------------------
# AC #3 — available_brokers is registry-driven, not hardcoded
# ---------------------------------------------------------------------------
def test_ac3_temporarily_registered_broker_appears_in_available_brokers():
    """Register a brand-new broker mid-test; assert it appears in
    available_brokers. Proves the endpoint reads the live registry rather
    than a hardcoded list."""
    app = create_app()

    ephemeral = type(
        "EphemeralBroker",
        (),
        {"broker_id": "ephemeral-fake-broker", "display_name": "Ephemeral Fake Broker"},
    )()
    with app.app_context():
        register_broker_connector(ephemeral)

    user_id = _unique_user_id("ac3-registry")
    client = _authenticated_client(app, user_id)
    response = client.get("/api/brokers/connections")

    assert response.status_code == 200
    body = response.get_json()
    broker_ids = {b["broker_id"] for b in body["available_brokers"]}
    assert "ephemeral-fake-broker" in broker_ids, (
        f"ephemeral broker missing — endpoint appears to use a hardcoded list. "
        f"Got: {broker_ids}"
    )
    # And its display_name matches what we registered
    ephemeral_entry = next(
        b for b in body["available_brokers"] if b["broker_id"] == "ephemeral-fake-broker"
    )
    assert ephemeral_entry["display_name"] == "Ephemeral Fake Broker"


# ---------------------------------------------------------------------------
# AC #4 — configured==False when UPSTOX_* env vars unset; endpoint still 200
# ---------------------------------------------------------------------------
def test_ac4_configured_false_when_upstox_env_vars_unset_endpoint_still_200():
    app = create_app()
    with app.app_context():
        register_broker_connector(
            type("UpstoxShim", (), {"broker_id": "upstox", "display_name": "Upstox"})()
        )

    user_id = _unique_user_id("ac4-unconfigured")

    # Strip every UPSTOX_* env var, scoped to this block.
    with unittest.mock.patch.dict(
        "os.environ",
        {k: v for k, v in os.environ.items() if not k.startswith("UPSTOX_")},
        clear=True,
    ):
        client = _authenticated_client(app, user_id)
        response = client.get("/api/brokers/connections")

    assert response.status_code == 200, (
        "endpoint must return 200 even when the broker is unconfigured "
        "(the disabled-button UX depends on it)"
    )
    body = response.get_json()
    upstox_entry = next(
        b for b in body["available_brokers"] if b["broker_id"] == "upstox"
    )
    assert upstox_entry["configured"] is False, (
        f"configured must be False when UPSTOX_* env vars are unset; got {upstox_entry}"
    )


# ---------------------------------------------------------------------------
# AC #5 — ERROR row exposes last_error
# ---------------------------------------------------------------------------
def test_ac5_error_connection_exposes_last_error():
    app = create_app()
    with app.app_context():
        register_broker_connector(
            type("UpstoxShim", (), {"broker_id": "upstox", "display_name": "Upstox"})()
        )

    user_id = _unique_user_id("ac5-error")
    _ensure_user_row(user_id)

    infra = DefaultInfrastructure()
    infra.upsert_broker_connection(
        user_id=user_id,
        broker_id="upstox",
        credentials=BrokerCredentials(access_token="x", token_type="Bearer"),
        status="CONNECTED",
    )
    infra.mark_broker_connection_error(
        user_id, "upstox", "real-token-expired-reconnect-please"
    )

    client = _authenticated_client(app, user_id)
    response = client.get("/api/brokers/connections")

    assert response.status_code == 200
    body = response.get_json()
    upstox = next(c for c in body["connections"] if c["broker_id"] == "upstox")
    assert upstox["status"] == "ERROR"
    assert upstox["last_error"] == "real-token-expired-reconnect-please"


# ---------------------------------------------------------------------------
# AC #6 — No access-token in response; 401 unauthenticated; per-user isolation
# ---------------------------------------------------------------------------
def test_ac6a_no_access_token_anywhere_in_response_body():
    app = create_app()
    with app.app_context():
        register_broker_connector(
            type("UpstoxShim", (), {"broker_id": "upstox", "display_name": "Upstox"})()
        )

    user_id = _unique_user_id("ac6a-token-leak")
    _ensure_user_row(user_id)
    # Plant a recognisable token we can grep for.
    unique_secret = f"unique-token-{uuid.uuid4().hex}"
    DefaultInfrastructure().upsert_broker_connection(
        user_id=user_id,
        broker_id="upstox",
        credentials=BrokerCredentials(
            access_token=unique_secret, token_type="Bearer"
        ),
        status="CONNECTED",
    )

    client = _authenticated_client(app, user_id)
    response = client.get("/api/brokers/connections")
    assert response.status_code == 200
    raw = response.get_data(as_text=True)

    assert unique_secret not in raw, (
        f"the access token '{unique_secret}' must not appear anywhere in the response"
    )
    assert "access_token" not in raw, (
        "no response field should be named 'access_token' (that's a secret field)"
    )
    assert "access_token_encrypted" not in raw, (
        "encrypted token ciphertext should also not be exposed to the UI"
    )


def test_ac6b_unauthenticated_request_returns_401():
    app = create_app()
    client = app.test_client()  # no session
    response = client.get("/api/brokers/connections")
    assert response.status_code == 401, (
        f"unauthenticated request must be 401, got {response.status_code}"
    )


def test_ac6c_one_user_cannot_see_another_users_connections():
    app = create_app()
    with app.app_context():
        register_broker_connector(
            type("UpstoxShim", (), {"broker_id": "upstox", "display_name": "Upstox"})()
        )

    user_a = _unique_user_id("ac6c-user-a")
    user_b = _unique_user_id("ac6c-user-b")
    _ensure_user_row(user_a)
    _ensure_user_row(user_b)

    # user_a has a CONNECTED upstox row.
    DefaultInfrastructure().upsert_broker_connection(
        user_id=user_a,
        broker_id="upstox",
        credentials=BrokerCredentials(access_token="only-a", token_type="Bearer"),
        status="CONNECTED",
    )

    # user_b queries the endpoint — must NOT see user_a's row.
    client_b = _authenticated_client(app, user_b)
    response_b = client_b.get("/api/brokers/connections")
    assert response_b.status_code == 200
    body_b = response_b.get_json()
    assert body_b["connections"] == [], (
        "user_b must not see user_a's connections — got: "
        f"{body_b['connections']}"
    )

    # And user_a does see their own row (sanity check the seed worked).
    client_a = _authenticated_client(app, user_a)
    response_a = client_a.get("/api/brokers/connections")
    body_a = response_a.get_json()
    assert any(c["broker_id"] == "upstox" for c in body_a["connections"]), (
        f"user_a's own row missing — seed/lookup broken. Got: {body_a['connections']}"
    )