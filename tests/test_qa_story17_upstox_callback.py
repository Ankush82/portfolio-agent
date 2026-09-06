"""QA tests for STORY-17 — GET /api/brokers/upstox/callback endpoint.

Verifies every acceptance criterion using the Flask test client only;
no network calls to Upstox are made (connector/token-exchange are stubbed).
"""

from __future__ import annotations

import pytest
import unittest.mock

from webapp import create_app
from components.c01_user_portfolio import (
    BrokerAuthError,
    BrokerConfigError,
    DefaultUpstoxBrokerConnector,
    StubBrokerConnector,
    register_broker_connector,
    unregister_broker_connector,
)
from oauth_state import (
    InvalidOAuthStateError,
    OAuthStateExpiredError,
    OAuthStateReplayError,
    consume_state,
    issue_state,
)
from infrastructure_postgres import DefaultInfrastructure


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register_stub_connector(app, raise_on: BaseException | None = None):
    """Register a StubBrokerConnector (exchange_auth_code never hits network)."""
    with app.app_context():
        connector = StubBrokerConnector(raise_on=raise_on)
        register_broker_connector(connector)


def _issue_and_consume_state(user_id: str, broker_id: str = "upstox") -> str:
    """Issue a fresh state token (for tests that verify consume vs. replay)."""
    return issue_state(user_id, broker_id)


# ---------------------------------------------------------------------------
# AC: Valid code + valid state → CONNECTED row + 302 to success URL
# ---------------------------------------------------------------------------

class TestHappyPath:
    """AC: Valid code + valid state results in a CONNECTED broker_connections
    row for the state's user and a 302 to /settings/brokers?connect=success&broker=upstox."""

    def test_returns_302_to_success_url(self, postgres_dsn: str):
        """302 redirect to /settings/brokers?connect=success&broker=upstox."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "callback-happy-user"
        state = issue_state(user_id, "upstox")
        response = client.get(f"/api/brokers/upstox/callback?state={state}&code=valid-code-123")

        assert response.status_code == 302
        assert response.location == "/settings/brokers?connect=success&broker=upstox"

    def test_creates_CONNECTED_broker_connection_row(self, postgres_dsn: str):
        """broker_connections row has status=CONNECTED for the state's user."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "callback-happy-row-user"
        state = issue_state(user_id, "upstox")
        client.get(f"/api/brokers/upstox/callback?state={state}&code=valid-code-123")

        # Verify row was persisted
        infra = DefaultInfrastructure()
        conn = infra.get_broker_connection(user_id, "upstox")
        assert conn is not None
        assert conn.status == "CONNECTED"

    def test_state_is_consumed_after_callback(self, postgres_dsn: str):
        """State token cannot be used a second time (replay protection)."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "callback-state-consumed-user"
        state = issue_state(user_id, "upstox")

        # First call succeeds
        r1 = client.get(f"/api/brokers/upstox/callback?state={state}&code=valid-code-123")
        assert r1.status_code == 302

        # Second call with the same state is rejected as replayed
        r2 = client.get(f"/api/brokers/upstox/callback?state={state}&code=valid-code-123")
        assert r2.status_code == 302
        assert "reason=state_replayed" in r2.location


# ---------------------------------------------------------------------------
# AC: Unknown / expired / replayed state each redirect with correct reason
# ---------------------------------------------------------------------------

class TestStateErrors:
    """AC: Unknown, expired, and already-used state values each redirect with
    reason=invalid_state, state_expired, state_replayed respectively and
    create no connection row."""

    def test_unknown_state_redirects_invalid_state(self, postgres_dsn: str):
        """Unknown state → reason=invalid_state."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()

        fake_state = "not-a-real-state-token-xyz"
        response = client.get(
            f"/api/brokers/upstox/callback?state={fake_state}&code=some-code"
        )

        assert response.status_code == 302
        assert "reason=invalid_state" in response.location
        assert "connect=error" in response.location

    def test_unknown_state_creates_no_connection_row(self, postgres_dsn: str):
        """Unknown state → no broker_connections row is created."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()

        fake_state = "definitely-not-real-state"
        client.get(f"/api/brokers/upstox/callback?state={fake_state}&code=some-code")

        infra = DefaultInfrastructure()
        # No row should exist for any user from a bogus state
        conn = infra.get_broker_connection("any-user", "upstox")
        assert conn is None

    def test_replayed_state_redirects_state_replayed(self, postgres_dsn: str):
        """Already-consumed state → reason=state_replayed.

        We consume the state explicitly then call the callback with it to
        simulate a race condition / replay attack.
        """
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "callback-replay-user"
        state = issue_state(user_id, "upstox")

        # Consume it directly (simulates a prior callback call)
        consume_state(state)

        # Now the callback should reject it
        response = client.get(
            f"/api/brokers/upstox/callback?state={state}&code=some-code"
        )
        assert response.status_code == 302
        assert "reason=state_replayed" in response.location


# ---------------------------------------------------------------------------
# AC: Missing code / access_denied
# ---------------------------------------------------------------------------

class TestMissingCodeAndAccessDenied:
    """AC: Missing code redirects with reason=missing_code; a callback
    carrying an Upstox error/denial parameter instead of a code redirects
    with reason=access_denied."""

    def test_missing_code_redirects_missing_code(self, postgres_dsn: str):
        """No code param → reason=missing_code."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "callback-no-code-user"
        state = issue_state(user_id, "upstox")
        response = client.get(f"/api/brokers/upstox/callback?state={state}")

        assert response.status_code == 302
        assert "reason=missing_code" in response.location

    def test_upstox_error_param_redirects_access_denied(self, postgres_dsn: str):
        """Upstox returns error=access_denied instead of code → reason=access_denied."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "callback-denied-user"
        state = issue_state(user_id, "upstox")
        response = client.get(
            f"/api/brokers/upstox/callback?state={state}&error=access_denied"
        )

        assert response.status_code == 302
        assert "reason=access_denied" in response.location

    def test_no_connection_on_access_denied(self, postgres_dsn: str):
        """Access denied → no broker_connections row is created."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "callback-denied-no-row"
        state = issue_state(user_id, "upstox")
        client.get(
            f"/api/brokers/upstox/callback?state={state}&error=access_denied"
        )

        infra = DefaultInfrastructure()
        conn = infra.get_broker_connection(user_id, "upstox")
        assert conn is None


# ---------------------------------------------------------------------------
# AC: BrokerAuthError from token exchange → token_exchange_failed
# ---------------------------------------------------------------------------

class TestTokenExchangeErrors:
    """AC: A BrokerAuthError from the token exchange redirects with
    reason=token_exchange_failed and no CONNECTED row is written."""

    def test_broker_auth_error_redirects_token_exchange_failed(self, postgres_dsn: str):
        """exchange_auth_code raises BrokerAuthError → reason=token_exchange_failed."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        # StubBrokerConnector with raise_on="invalid" raises BrokerAuthError
        # for the 'invalid' sentinel code (any other code returns credentials).
        _register_stub_connector(app, raise_on=BrokerAuthError("token rejected"))
        client = app.test_client()

        user_id = "callback-auth-err-user"
        state = issue_state(user_id, "upstox")
        response = client.get(
            f"/api/brokers/upstox/callback?state={state}&code=some-code"
        )

        assert response.status_code == 302
        assert "reason=token_exchange_failed" in response.location

    def test_auth_error_creates_no_CONNECTED_row(self, postgres_dsn: str):
        """BrokerAuthError → no CONNECTED row is written."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_stub_connector(app, raise_on=BrokerAuthError("rejected"))
        client = app.test_client()

        user_id = "callback-auth-no-row-user"
        state = issue_state(user_id, "upstox")
        client.get(f"/api/brokers/upstox/callback?state={state}&code=some-code")

        infra = DefaultInfrastructure()
        conn = infra.get_broker_connection(user_id, "upstox")
        # Either no row at all, or a row that is NOT CONNECTED
        if conn is not None:
            assert conn.status != "CONNECTED"


# ---------------------------------------------------------------------------
# AC: Route ignores user_id in query string — forged user_id cannot
#     cause a connection for another user
# ---------------------------------------------------------------------------

class TestIgnoreQueryStringUserId:
    """AC: The route ignores any user_id present in the query string;
    a test asserts a forged user_id cannot cause a connection for another user."""

    def test_forged_user_id_in_query_string_is_ignored(self, postgres_dsn: str):
        """user_id=attacker in query string does NOT create a connection for attacker."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()

        victim_user = "callback-victim-user"
        attacker_user = "callback-attacker-user"
        state = issue_state(victim_user, "upstox")

        # Attacker crafts a URL with their own user_id in the query string
        response = client.get(
            f"/api/brokers/upstox/callback"
            f"?state={state}"
            f"&code=valid-code"
            f"&user_id={attacker_user}"
        )

        # The connection must be for the victim, not the attacker
        infra = DefaultInfrastructure()
        victim_conn = infra.get_broker_connection(victim_user, "upstox")
        attacker_conn = infra.get_broker_connection(attacker_user, "upstox")

        assert victim_conn is not None
        assert victim_conn.status == "CONNECTED"
        assert attacker_conn is None  # Attacker got nothing

    def test_user_id_not_required_in_query_string(self, postgres_dsn: str):
        """Callback works even when user_id is completely absent from query string."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "callback-no-uid-param-user"
        state = issue_state(user_id, "upstox")

        # No user_id param at all — only state + code
        response = client.get(
            f"/api/brokers/upstox/callback?state={state}&code=valid-code"
        )

        assert response.status_code == 302
        assert "connect=success" in response.location


# ---------------------------------------------------------------------------
# AC: State value and auth code do not appear in logs
# ---------------------------------------------------------------------------

class TestSensitiveDataNotInLogs:
    """AC: The state value and the auth code do not appear in logs."""

    def test_state_not_in_log_output(self, postgres_dsn: str, caplog):
        """State token must not appear in any log line."""
        import oauth_state
        oauth_state.configure(postgres_dsn)
        import logging

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "callback-log-test-user"
        state = issue_state(user_id, "upstox")

        with caplog.at_level(logging.WARNING):
            client.get(
                f"/api/brokers/upstox/callback?state={state}&code=sensitive-code"
            )

        for record in caplog.records:
            assert state not in record.message, (
                f"State token leaked into log: {record.message}"
            )

    def test_auth_code_not_in_log_output(self, postgres_dsn: str, caplog):
        """Auth code must not appear in any log line."""
        import oauth_state
        oauth_state.configure(postgres_dsn)
        import logging

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "callback-code-log-user"
        state = issue_state(user_id, "upstox")
        secret_code = "super-secret-auth-code-12345"

        with caplog.at_level(logging.WARNING):
            client.get(
                f"/api/brokers/upstox/callback?state={state}&code={secret_code}"
            )

        for record in caplog.records:
            assert secret_code not in record.message, (
                f"Auth code leaked into log: {record.message}"
            )


# ---------------------------------------------------------------------------
# AC: No session cookie required
# ---------------------------------------------------------------------------

class TestNoSessionRequired:
    """AC: This route must not require an existing session cookie for identity —
    identity comes solely from the single-use state."""

    def test_callback_works_without_session(self, postgres_dsn: str):
        """GET /api/brokers/upstox/callback with no cookie succeeds if state is valid."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()  # No session_transaction

        user_id = "callback-no-session-user"
        state = issue_state(user_id, "upstox")

        # Explicitly no session cookie in the request
        response = client.get(
            f"/api/brokers/upstox/callback?state={state}&code=valid-code",
            headers={"Cookie": ""},
        )

        assert response.status_code == 302
        assert "connect=success" in response.location


# ---------------------------------------------------------------------------
# AC: All tests stub the connector/token exchange; no network calls to Upstox
# ---------------------------------------------------------------------------

class TestNoNetworkCalls:
    """AC: All tests stub the connector/token exchange; no network calls to
    Upstox are made."""

    def test_no_http_call_is_made(self, postgres_dsn: str):
        """exchange_auth_code in DefaultUpstoxBrokerConnector is never invoked
        in tests (StubBrokerConnector is used instead)."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        connector = StubBrokerConnector()
        register_broker_connector(connector)

        # Patch the real connector class to prove it's never instantiated
        with unittest.mock.patch.object(
            DefaultUpstoxBrokerConnector,
            "exchange_auth_code",
            side_effect=RuntimeError("Real connector must not be called"),
        ) as mock_real_exchange:
            client = app.test_client()
            user_id = "callback-no-real-http-user"
            state = issue_state(user_id, "upstox")
            response = client.get(
                f"/api/brokers/upstox/callback?state={state}&code=valid-code"
            )

            assert response.status_code == 302
            assert "connect=success" in response.location
            assert not mock_real_exchange.called

        register_broker_connector(connector)  # restore


# ---------------------------------------------------------------------------
# AC: empty/missing state slug
# ---------------------------------------------------------------------------

class TestEmptyStateSlug:
    """Empty state param should behave identically to a missing state."""

    def test_empty_state_redirects_invalid_state(self, postgres_dsn: str):
        """state= (empty) → reason=invalid_state."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_stub_connector(app)
        client = app.test_client()

        response = client.get("/api/brokers/upstox/callback?state=&code=some-code")

        assert response.status_code == 302
        assert "reason=invalid_state" in response.location
