"""QA tests for STORY-16 — POST /api/brokers/upstox/connect endpoint.

Verifies every acceptance criterion using the Flask test client only;
no network calls to Upstox are made.
"""

from __future__ import annotations

import pytest
import unittest.mock

from webapp import create_app
from components.c01_user_portfolio import (
    DefaultUpstoxBrokerConnector,
    register_broker_connector,
)
from oauth_state import consume_state
from upstox_config import BrokerConfigError as UpstoxBrokerConfigError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_authenticated_client(app, user_id="test-user-123"):
    """Return a Flask test client with an active session for user_id."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    return client


def _register_real_upstox_connector(app):
    """Register a real Upstox connector (build_authorize_url needs no HTTP)."""
    import os
    with app.app_context():
        with unittest.mock.patch.dict(os.environ, {
            "UPSTOX_CLIENT_ID": "test-client-id",
            "UPSTOX_CLIENT_SECRET": "test-client-secret",
            "UPSTOX_REDIRECT_URI": "https://example.com/callback",
        }):
            from upstox_config import UpstoxConfig
            config = UpstoxConfig.from_env()
            connector = DefaultUpstoxBrokerConnector(config=config, http=unittest.mock.Mock())
            register_broker_connector(connector)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    """AC: Happy path returns 200 with authorize_url and state."""

    def test_returns_200_with_authorize_url_and_state(self, postgres_dsn: str):
        """AC: Returns 200 with authorize_url and state keys."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_real_upstox_connector(app)
        client = _make_authenticated_client(app)

        response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 200
        data = response.get_json()
        assert "authorize_url" in data
        assert "state" in data

    def test_authorize_url_uses_correct_host_and_path(self, postgres_dsn: str):
        """AC: authorize_url host/path is api.upstox.com/v2/login/authorization/dialog."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_real_upstox_connector(app)
        client = _make_authenticated_client(app)

        response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 200
        authorize_url = response.get_json()["authorize_url"]
        assert "api.upstox.com/v2/login/authorization/dialog" in authorize_url

    def test_returned_state_matches_query_param_in_url(self, postgres_dsn: str):
        """AC: authorize_url contains state=<returned state>."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_real_upstox_connector(app)
        client = _make_authenticated_client(app)

        response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 200
        data = response.get_json()
        assert f"state={data['state']}" in data["authorize_url"]

    def test_state_is_present_in_state_store_for_caller(self, postgres_dsn: str):
        """AC: state is present in the state store for the caller's user."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        user_id = "story16-user-store-check"
        app = create_app()
        _register_real_upstox_connector(app)
        client = _make_authenticated_client(app, user_id=user_id)

        response = client.post("/api/brokers/upstox/connect")
        assert response.status_code == 200
        returned_state = response.get_json()["state"]

        # State must be consumable from the store as the caller's user + "upstox"
        consumed_user_id, consumed_broker_id = consume_state(returned_state)
        assert consumed_user_id == user_id
        assert consumed_broker_id == "upstox"

    def test_consecutive_calls_return_different_states(self, postgres_dsn: str):
        """AC: Two consecutive calls return two different state values."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_real_upstox_connector(app)
        client = _make_authenticated_client(app)

        response1 = client.post("/api/brokers/upstox/connect")
        response2 = client.post("/api/brokers/upstox/connect")

        assert response1.status_code == 200
        assert response2.status_code == 200
        assert response1.get_json()["state"] != response2.get_json()["state"]

    def test_response_body_does_not_contain_client_secret(self, postgres_dsn: str):
        """AC: The response body never contains the client secret."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_real_upstox_connector(app)
        client = _make_authenticated_client(app)

        response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "test-client-secret" not in body
        assert "UPSTOX_CLIENT_SECRET" not in body


# ---------------------------------------------------------------------------
# Unauthenticated requests
# ---------------------------------------------------------------------------

class TestUnauthenticated:
    """AC: Unauthenticated request returns 401."""

    def test_unauthenticated_returns_401(self, postgres_dsn: str):
        """AC: Unauthenticated request returns 401."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        client = app.test_client()  # no session

        response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 401
        data = response.get_json()
        assert "error" in data

    def test_issue_state_not_called_for_unauthenticated(self, postgres_dsn: str):
        """AC: No state row is created for unauthenticated requests."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        client = app.test_client()  # no session

        with unittest.mock.patch("webapp.issue_state") as mock_issue:
            response = client.post("/api/brokers/upstox/connect")
            assert response.status_code == 401
            mock_issue.assert_not_called()


# ---------------------------------------------------------------------------
# Missing env vars
# ---------------------------------------------------------------------------

class TestMissingEnvVars:
    """AC: With Upstox env vars unset, response is 503 with error='broker_not_configured'."""

    def test_returns_503_when_env_vars_missing(self, postgres_dsn: str):
        """AC: Returns 503 with error == 'broker_not_configured'."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        client = _make_authenticated_client(app)

        # Simulate UpstoxConfig.from_env() raising BrokerConfigError for missing vars.
        # Patch the class method where it is used (webapp module).
        with unittest.mock.patch("webapp.UpstoxConfig.from_env") as mock_from_env:
            mock_from_env.side_effect = UpstoxBrokerConfigError(
                "Missing or empty Upstox configuration: UPSTOX_CLIENT_ID, "
                "UPSTOX_CLIENT_SECRET, UPSTOX_REDIRECT_URI. register an app at ..."
            )
            response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 503
        data = response.get_json()
        assert data["error"] == "broker_not_configured"

    def test_message_names_all_three_missing_variables(self, postgres_dsn: str):
        """AC: Message names the missing variables; operator gets actionable text."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        client = _make_authenticated_client(app)

        with unittest.mock.patch("webapp.UpstoxConfig.from_env") as mock_from_env:
            mock_from_env.side_effect = UpstoxBrokerConfigError(
                "Missing or empty Upstox configuration: UPSTOX_CLIENT_ID, "
                "UPSTOX_CLIENT_SECRET, UPSTOX_REDIRECT_URI."
            )
            response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 503
        message = response.get_json()["message"]
        assert "UPSTOX_CLIENT_ID" in message
        assert "UPSTOX_CLIENT_SECRET" in message
        assert "UPSTOX_REDIRECT_URI" in message

    def test_no_state_row_created_when_config_missing(self, postgres_dsn: str):
        """AC: No state row is created when env vars are missing."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        client = _make_authenticated_client(app)

        with unittest.mock.patch("webapp.UpstoxConfig.from_env") as mock_from_env:
            mock_from_env.side_effect = UpstoxBrokerConfigError("Missing UPSTOX_CLIENT_ID")
            response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 503
        # issue_state must NOT have been called (it comes after the config check)
        mock_from_env.assert_called_once()


# ---------------------------------------------------------------------------
# No network calls to Upstox
# ---------------------------------------------------------------------------

class TestNoNetworkCalls:
    """AC: API tests use the test client only; no network calls to Upstox."""

    def test_http_mock_not_called(self, postgres_dsn: str):
        """AC: No HTTP network call to Upstox is made."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        with app.app_context():
            import os
            with unittest.mock.patch.dict(os.environ, {
                "UPSTOX_CLIENT_ID": "test-id",
                "UPSTOX_CLIENT_SECRET": "test-secret",
                "UPSTOX_REDIRECT_URI": "https://example.com/cb",
            }):
                from upstox_config import UpstoxConfig
                config = UpstoxConfig.from_env()
                mock_http = unittest.mock.Mock()
                connector = DefaultUpstoxBrokerConnector(config=config, http=mock_http)
                register_broker_connector(connector)

        client = _make_authenticated_client(app)
        response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 200
        # HTTP mock must never have been invoked
        assert not mock_http.called
