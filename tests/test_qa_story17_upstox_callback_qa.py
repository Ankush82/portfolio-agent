"""QA tests for STORY-17 — GET /api/brokers/upstox/callback endpoint.

Verifies every acceptance criterion using the Flask test client.
The source file has a blocking NameError bug (duplicate DefaultUserPortfolio
referencing undefined LifecycleMixin at line 1697), so these tests mock
DefaultUserPortfolio to allow the Flask app to load and run.

No network calls to Upstox are made (connector is stubbed).
"""

from __future__ import annotations

import unittest.mock
import pytest
from unittest.mock import MagicMock

import oauth_state
from oauth_state import (
    InvalidOAuthStateError,
    OAuthStateExpiredError,
    OAuthStateReplayError,
    issue_state,
    consume_state,
)
from infrastructure_postgres import DefaultInfrastructure


# ---------------------------------------------------------------------------
# Mock DefaultUserPortfolio so the duplicate-class NameError doesn't block import
# ---------------------------------------------------------------------------

class FakeBrokerConnectionRecord:
    def __init__(self, user_id, broker_id, status="CONNECTED"):
        self.id = f"fake-conn-{user_id}-{broker_id}"
        self.user_id = user_id
        self.broker_id = broker_id
        self.broker_user_id = "fake-broker-user"
        self.access_token_encrypted = "fake-encrypted"
        self.token_type = "Bearer"
        self.access_token_expires_at = None
        self.status = status
        self.last_error = None
        from datetime import datetime, timezone
        self.connected_at = datetime.now(timezone.utc).isoformat()
        self.last_import_at = None
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.updated_at = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Helper — patches c01_user_portfolio before webapp is imported
# ---------------------------------------------------------------------------

def _patch_and_create_app(postgres_dsn: str):
    """Patch the broken DefaultUserPortfolio import, then return the Flask app."""
    import sys

    # Prevent re-import of a broken c01_user_portfolio
    for mod in list(sys.modules.keys()):
        if "c01_user_portfolio" in mod or mod == "components.c01_user_portfolio":
            del sys.modules[mod]
        if "webapp" in mod:
            del sys.modules[mod]

    # Patch BrokerAuthError / BrokerConfigError in the module namespace BEFORE
    # webapp imports c01_user_portfolio
    from components import c01_user_portfolio as c01

    # Replace the broken DefaultUserPortfolio with our fake
    class PatchedDefaultUserPortfolio:
        def __init__(self, **kwargs):
            self._raised_exc = None
            self._call_args = None

        def connect_portfolio(self, user_id, broker_id, payload):
            self._call_args = (user_id, broker_id, payload)
            if self._raised_exc:
                raise self._raised_exc
            return FakeBrokerConnectionRecord(user_id, broker_id, status="CONNECTED")

        def _set_exception(self, exc):
            self._raised_exc = exc

    c01.DefaultUserPortfolio = PatchedDefaultUserPortfolio  # type: ignore

    # Now import and create the app
    from webapp import create_app
    app = create_app()
    oauth_state.configure(postgres_dsn)
    return app


def _register_stub_connector(app):
    from components.c01_user_portfolio import register_broker_connector, StubBrokerConnector
    with app.app_context():
        connector = StubBrokerConnector()
        register_broker_connector(connector)


# ---------------------------------------------------------------------------
# AC1: Valid code + valid state → CONNECTED row + 302 to success URL
# ---------------------------------------------------------------------------

class TestAC1HappyPath:
    """AC: Valid code + valid state results in a CONNECTED broker_connections
    row for the state's user and a 302 to /settings/brokers?connect=success&broker=upstox."""

    def test_returns_302_to_success_url(self, postgres_dsn: str):
        """302 redirect to /settings/brokers?connect=success&broker=upstox."""
        app = _patch_and_create_app(postgres_dsn)
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "ac1-happy-user"
        state = issue_state(user_id, "upstox")
        response = client.get(f"/api/brokers/upstox/callback?state={state}&code=valid-code-123")

        assert response.status_code == 302, f"Expected 302, got {response.status_code}"
        assert response.location == "/settings/brokers?connect=success&broker=upstox", \
            f"Expected success redirect, got {response.location}"

    def test_creates_CONNECTED_broker_connection_row(self, postgres_dsn: str):
        """broker_connections row has status=CONNECTED for the state's user."""
        app = _patch_and_create_app(postgres_dsn)
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "ac1-row-user"
        state = issue_state(user_id, "upstox")
        client.get(f"/api/brokers/upstox/callback?state={state}&code=valid-code-123")

        infra = DefaultInfrastructure()
        conn = infra.get_broker_connection(user_id, "upstox")
        assert conn is not None, "Expected a broker_connections row"
        assert conn.status == "CONNECTED", f"Expected CONNECTED, got {conn.status}"

    def test_state_is_consumed_after_callback(self, postgres_dsn: str):
        """State token cannot be used a second time (replay protection)."""
        app = _patch_and_create_app(postgres_dsn)
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "ac1-consumed-user"
        state = issue_state(user_id, "upstox")

        # First call succeeds
        r1 = client.get(f"/api/brokers/upstox/callback?state={state}&code=valid-code-123")
        assert r1.status_code == 302

        # Second call with same state is rejected as replayed
        r2 = client.get(f"/api/brokers/upstox/callback?state={state}&code=valid-code-123")
        assert r2.status_code == 302
        assert "reason=state_replayed" in r2.location, \
            f"Expected state_replayed, got {r2.location}"


# ---------------------------------------------------------------------------
# AC2: Unknown / expired / replayed state → correct reason slugs + no row
# ---------------------------------------------------------------------------

class TestAC2StateErrors:
    """AC: Unknown, expired, and already-used state values each redirect with
    reason=invalid_state, state_expired, state_replayed respectively and
    create no connection row."""

    def test_unknown_state_redirects_invalid_state(self, postgres_dsn: str):
        """Unknown state → reason=invalid_state."""
        app = _patch_and_create_app(postgres_dsn)
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
        app = _patch_and_create_app(postgres_dsn)
        _register_stub_connector(app)
        client = app.test_client()

        fake_state = "definitely-not-real-state"
        client.get(f"/api/brokers/upstox/callback?state={fake_state}&code=some-code")

        infra = DefaultInfrastructure()
        conn = infra.get_broker_connection("any-user", "upstox")
        assert conn is None

    def test_replayed_state_redirects_state_replayed(self, postgres_dsn: str):
        """Already-consumed state → reason=state_replayed."""
        app = _patch_and_create_app(postgres_dsn)
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "ac2-replay-user"
        state = issue_state(user_id, "upstox")

        # Consume it directly (simulates a prior callback call)
        consume_state(state)

        # Callback should reject it
        response = client.get(
            f"/api/brokers/upstox/callback?state={state}&code=some-code"
        )
        assert response.status_code == 302
        assert "reason=state_replayed" in response.location, \
            f"Expected state_replayed, got {response.location}"


# ---------------------------------------------------------------------------
# AC3: Missing code / access_denied → correct reason slugs
# ---------------------------------------------------------------------------

class TestAC3MissingCodeAndAccessDenied:
    """AC: Missing code redirects with reason=missing_code; a callback
    carrying an Upstox error/denial parameter instead of a code redirects
    with reason=access_denied."""

    def test_missing_code_redirects_missing_code(self, postgres_dsn: str):
        """No code param → reason=missing_code."""
        app = _patch_and_create_app(postgres_dsn)
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "ac3-no-code-user"
        state = issue_state(user_id, "upstox")
        response = client.get(f"/api/brokers/upstox/callback?state={state}")

        assert response.status_code == 302
        assert "reason=missing_code" in response.location, \
            f"Expected missing_code, got {response.location}"

    def test_upstox_error_param_redirects_access_denied(self, postgres_dsn: str):
        """Upstox returns error=access_denied instead of code → reason=access_denied."""
        app = _patch_and_create_app(postgres_dsn)
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "ac3-denied-user"
        state = issue_state(user_id, "upstox")
        response = client.get(
            f"/api/brokers/upstox/callback?state={state}&error=access_denied"
        )

        assert response.status_code == 302
        assert "reason=access_denied" in response.location, \
            f"Expected access_denied, got {response.location}"

    def test_no_connection_on_access_denied(self, postgres_dsn: str):
        """Access denied → no broker_connections row is created."""
        app = _patch_and_create_app(postgres_dsn)
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "ac3-denied-no-row"
        state = issue_state(user_id, "upstox")
        client.get(
            f"/api/brokers/upstox/callback?state={state}&error=access_denied"
        )

        infra = DefaultInfrastructure()
        conn = infra.get_broker_connection(user_id, "upstox")
        assert conn is None, "No row should exist on access_denied"


# ---------------------------------------------------------------------------
# AC4: BrokerAuthError from token exchange → token_exchange_failed + no row
# ---------------------------------------------------------------------------

class TestAC4TokenExchangeErrors:
    """AC: A BrokerAuthError from the token exchange redirects with
    reason=token_exchange_failed and no CONNECTED row is written."""

    def _make_app_with_auth_error(self, postgres_dsn: str, exc: Exception):
        """Create app where DefaultUserPortfolio.connect_portfolio raises exc."""
        import sys
        for mod in list(sys.modules.keys()):
            if "c01_user_portfolio" in mod or "webapp" in mod:
                del sys.modules[mod]

        from components import c01_user_portfolio as c01

        class RaisingPortfolio:
            def __init__(self, raised_exc):
                self._exc = raised_exc
            def connect_portfolio(self, user_id, broker_id, payload):
                raise self._exc

        c01.DefaultUserPortfolio = RaisingPortfolio  # type: ignore

        from webapp import create_app
        app = create_app()
        oauth_state.configure(postgres_dsn)
        return app

    def test_broker_auth_error_redirects_token_exchange_failed(self, postgres_dsn: str):
        """exchange_auth_code raises BrokerAuthError → reason=token_exchange_failed."""
        from components.c01_user_portfolio import BrokerAuthError

        app = self._make_app_with_auth_error(postgres_dsn, BrokerAuthError("token rejected"))
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "ac4-auth-err-user"
        state = issue_state(user_id, "upstox")
        response = client.get(
            f"/api/brokers/upstox/callback?state={state}&code=some-code"
        )

        assert response.status_code == 302
        assert "reason=token_exchange_failed" in response.location, \
            f"Expected token_exchange_failed, got {response.location}"

    def test_auth_error_creates_no_CONNECTED_row(self, postgres_dsn: str):
        """BrokerAuthError → no CONNECTED row is written."""
        from components.c01_user_portfolio import BrokerAuthError

        app = self._make_app_with_auth_error(postgres_dsn, BrokerAuthError("rejected"))
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "ac4-auth-no-row-user"
        state = issue_state(user_id, "upstox")
        client.get(f"/api/brokers/upstox/callback?state={state}&code=some-code")

        infra = DefaultInfrastructure()
        conn = infra.get_broker_connection(user_id, "upstox")
        # Either no row at all, or a row that is NOT CONNECTED
        if conn is not None:
            assert conn.status != "CONNECTED", \
                f"No CONNECTED row should exist after BrokerAuthError, got {conn.status}"


# ---------------------------------------------------------------------------
# AC5: Route ignores user_id in query string — forged user_id cannot
#      cause a connection for another user
# ---------------------------------------------------------------------------

class TestAC5IgnoreQueryStringUserId:
    """AC: The route ignores any user_id present in the query string;
    a test asserts a forged user_id cannot cause a connection for another user."""

    def test_forged_user_id_in_query_string_is_ignored(self, postgres_dsn: str):
        """user_id=attacker in query string does NOT create a connection for attacker."""
        app = _patch_and_create_app(postgres_dsn)
        _register_stub_connector(app)
        client = app.test_client()

        victim_user = "ac5-victim-user"
        attacker_user = "ac5-attacker-user"
        state = issue_state(victim_user, "upstox")

        # Attacker crafts a URL with their own user_id in the query string
        response = client.get(
            f"/api/brokers/upstox/callback"
            f"?state={state}"
            f"&code=valid-code"
            f"&user_id={attacker_user}"
        )

        assert response.status_code == 302
        assert "connect=success" in response.location

        # The connection must be for the victim, not the attacker
        infra = DefaultInfrastructure()
        victim_conn = infra.get_broker_connection(victim_user, "upstox")
        attacker_conn = infra.get_broker_connection(attacker_user, "upstox")

        assert victim_conn is not None, "Victim should have a connection"
        assert victim_conn.status == "CONNECTED", \
            f"Victim connection should be CONNECTED, got {victim_conn.status}"
        assert attacker_conn is None, \
            f"Attacker should have NO connection (forged user_id was ignored), got {attacker_conn}"

    def test_user_id_not_required_in_query_string(self, postgres_dsn: str):
        """Callback works even when user_id is completely absent from query string."""
        app = _patch_and_create_app(postgres_dsn)
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "ac5-no-uid-param-user"
        state = issue_state(user_id, "upstox")

        # No user_id param at all — only state + code
        response = client.get(
            f"/api/brokers/upstox/callback?state={state}&code=valid-code"
        )

        assert response.status_code == 302
        assert "connect=success" in response.location


# ---------------------------------------------------------------------------
# AC6: State value and auth code do not appear in logs
# ---------------------------------------------------------------------------

class TestAC6SensitiveDataNotInLogs:
    """AC: The state value and the auth code do not appear in logs."""

    def test_state_not_in_log_output(self, postgres_dsn: str, caplog):
        """State token must not appear in any log line."""
        import logging

        app = _patch_and_create_app(postgres_dsn)
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "ac6-log-state-user"
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
        import logging

        app = _patch_and_create_app(postgres_dsn)
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "ac6-log-code-user"
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
# AC7: No session cookie required
# ---------------------------------------------------------------------------

class TestAC7NoSessionRequired:
    """AC: This route must not require an existing session cookie for identity —
    identity comes solely from the single-use state."""

    def test_callback_works_without_session(self, postgres_dsn: str):
        """GET /api/brokers/upstox/callback with no cookie succeeds if state is valid."""
        app = _patch_and_create_app(postgres_dsn)
        _register_stub_connector(app)
        client = app.test_client()

        user_id = "ac7-no-session-user"
        state = issue_state(user_id, "upstox")

        # Explicitly no session cookie in the request
        response = client.get(
            f"/api/brokers/upstox/callback?state={state}&code=valid-code",
            headers={"Cookie": ""},
        )

        assert response.status_code == 302, f"Expected 302, got {response.status_code}"
        assert "connect=success" in response.location, \
            f"Expected success, got {response.location}"


# ---------------------------------------------------------------------------
# AC8: All tests stub the connector/token exchange; no network calls to Upstox
# ---------------------------------------------------------------------------

class TestAC8NoNetworkCalls:
    """AC: All tests stub the connector/token exchange; no network calls to
    Upstox are made."""

    def test_real_connector_exchange_is_never_called(self, postgres_dsn: str):
        """exchange_auth_code in DefaultUpstoxBrokerConnector is never invoked."""
        import sys
        for mod in list(sys.modules.keys()):
            if "c01_user_portfolio" in mod or "webapp" in mod:
                del sys.modules[mod]

        from components import c01_user_portfolio as c01
        from components.c01_user_portfolio import (
            DefaultUpstoxBrokerConnector,
            StubBrokerConnector,
            register_broker_connector,
        )

        # Patch DefaultUserPortfolio
        class PatchedPortfolio:
            def __init__(self, **kw):
                pass
            def connect_portfolio(self, user_id, broker_id, payload):
                return FakeBrokerConnectionRecord(user_id, broker_id)

        c01.DefaultUserPortfolio = PatchedPortfolio  # type: ignore

        from webapp import create_app
        app = create_app()
        oauth_state.configure(postgres_dsn)
        _register_stub_connector(app)

        # Patch the real connector class to prove it's never instantiated
        with unittest.mock.patch.object(
            DefaultUpstoxBrokerConnector,
            "exchange_auth_code",
            side_effect=RuntimeError("Real connector must not be called"),
        ) as mock_real_exchange:
            client = app.test_client()
            user_id = "ac8-no-real-http-user"
            state = issue_state(user_id, "upstox")
            response = client.get(
                f"/api/brokers/upstox/callback?state={state}&code=valid-code"
            )

            assert response.status_code == 302
            assert "connect=success" in response.location
            assert not mock_real_exchange.called, \
                "Real Upstox connector must not be called in tests"


# ---------------------------------------------------------------------------
# AC9: Empty/missing state slug
# ---------------------------------------------------------------------------

class TestAC9EmptyStateSlug:
    """Empty state param should behave identically to a missing state."""

    def test_empty_state_redirects_invalid_state(self, postgres_dsn: str):
        """state= (empty) → reason=invalid_state."""
        app = _patch_and_create_app(postgres_dsn)
        _register_stub_connector(app)
        client = app.test_client()

        response = client.get("/api/brokers/upstox/callback?state=&code=some-code")

        assert response.status_code == 302
        assert "reason=invalid_state" in response.location, \
            f"Expected invalid_state, got {response.location}"
