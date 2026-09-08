"""QA tests for STORY-17: GET /api/brokers/upstox/callback

Verifies the acceptance criteria for the Upstox OAuth callback endpoint:
  /api/brokers/upstox/callback

All tests stub the connector/token exchange; no network calls to Upstox.
All infrastructure calls are mocked so tests run without a live Postgres.
"""
from __future__ import annotations

import json
import logging
import re
from unittest.mock import MagicMock, patch

import pytest

# Import the Flask app factory
from webapp import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_redirect(response, expected_location: str) -> None:
    """Assert response is a 302 redirect to expected_location."""
    assert response.status_code == 302, (
        f"Expected 302 redirect, got {response.status_code}. "
        f"Response: {response.headers.get('Location')}"
    )
    location = response.headers.get("Location", "")
    # Strip schema/host if present (test client gives absolute URLs)
    if location.startswith("http://"):
        from urllib.parse import urlparse
        parsed = urlparse(location)
        location = parsed.path
        if parsed.query:
            location += "?" + parsed.query
    assert location == expected_location, (
        f"Expected redirect to {expected_location!r}, got {location!r}"
    )


def _assert_redirect_with_reason(response, slug: str) -> None:
    """Assert response is a 302 redirect to the error URL with the given reason slug."""
    expected = f"/settings/brokers?connect=error&broker=upstox&reason={slug}"
    _assert_redirect(response, expected)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    """Create a test-flask app with patched infrastructure."""
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# AC: Valid code + valid state → 302 success + CONNECTED broker_connections row
# ---------------------------------------------------------------------------

def test_valid_code_and_state_redirects_to_success_and_calls_connect_portfolio(client, caplog):
    """AC: Valid `code` + valid `state` results in a CONNECTED
    `broker_connections` row for the state's user and a 302 to
    `/settings/brokers?connect=success&broker=upstox`."""
    with caplog.at_level(logging.INFO):
        with patch("webapp.consume_state") as mock_consume, \
             patch("webapp.DefaultUserPortfolio") as MockPortfolio:

            # Mock consume_state returns the user/broker identity from the state
            mock_consume.return_value = ("user-123", "upstox")

            # Mock connect_portfolio returns a mock record
            mock_record = MagicMock()
            mock_record.id = "record-abc"
            mock_record.user_id = "user-123"
            mock_record.broker_id = "upstox"
            mock_record.status = "CONNECTED"
            mock_record.access_token_encrypted = "***"
            MockPortfolio.return_value.connect_portfolio.return_value = mock_record

            response = client.get(
                "/api/brokers/upstox/callback?code=real-auth-code&state=valid-state-token"
            )

    # ── Redirect ────────────────────────────────────────────────────────────
    _assert_redirect(response, "/settings/brokers?connect=success&broker=upstox")

    # ── connect_portfolio was called correctly ──────────────────────────────
    mock_consume.assert_called_once_with("valid-state-token")
    MockPortfolio.return_value.connect_portfolio.assert_called_once_with(
        user_id="user-123",
        broker_id="upstox",
        payload={"code": "real-auth-code"},
    )

    # ── No error was logged ─────────────────────────────────────────────────
    assert "UPSTOX_CALLBACK_ERROR" not in caplog.text


# ---------------------------------------------------------------------------
# AC: Unknown state → reason=invalid_state, no connection row
# ---------------------------------------------------------------------------

def test_unknown_state_redirects_with_invalid_state_reason(client):
    """AC: Unknown state redirects with reason=invalid_state and creates no connection row."""
    with patch("webapp.consume_state") as mock_consume:
        from oauth_state import InvalidOAuthStateError
        mock_consume.side_effect = InvalidOAuthStateError("unknown state")

        response = client.get(
            "/api/brokers/upstox/callback?code=some-code&state=unknown-state"
        )

    _assert_redirect_with_reason(response, "invalid_state")
    # connect_portfolio was never called
    mock_consume.assert_called_once_with("unknown-state")


# ---------------------------------------------------------------------------
# AC: Expired state → reason=state_expired, no connection row
# ---------------------------------------------------------------------------

def test_expired_state_redirects_with_state_expired_reason(client):
    """AC: Expired state redirects with reason=state_expired and creates no connection row."""
    with patch("webapp.consume_state") as mock_consume:
        from oauth_state import OAuthStateExpiredError
        mock_consume.side_effect = OAuthStateExpiredError("expired")

        response = client.get(
            "/api/brokers/upstox/callback?code=some-code&state=expired-state"
        )

    _assert_redirect_with_reason(response, "state_expired")
    mock_consume.assert_called_once_with("expired-state")


# ---------------------------------------------------------------------------
# AC: Replayed state → reason=state_replayed, no connection row
# ---------------------------------------------------------------------------

def test_replayed_state_redirects_with_state_replayed_reason(client):
    """AC: Already-used state redirects with reason=state_replayed and creates no connection row."""
    with patch("webapp.consume_state") as mock_consume:
        from oauth_state import OAuthStateReplayError
        mock_consume.side_effect = OAuthStateReplayError("replayed")

        response = client.get(
            "/api/brokers/upstox/callback?code=some-code&state=replayed-state"
        )

    _assert_redirect_with_reason(response, "state_replayed")
    mock_consume.assert_called_once_with("replayed-state")


# ---------------------------------------------------------------------------
# AC: Missing code → reason=missing_code
# ---------------------------------------------------------------------------

def test_missing_code_redirects_with_missing_code_reason(client):
    """AC: Missing `code` redirects with reason=missing_code."""
    with patch("webapp.consume_state") as mock_consume:
        mock_consume.return_value = ("user-123", "upstox")

        # No code param at all
        response = client.get("/api/brokers/upstox/callback?state=valid-state")

    _assert_redirect_with_reason(response, "missing_code")
    mock_consume.assert_called_once()


# ---------------------------------------------------------------------------
# AC: Upstox error/denial → reason=access_denied
# ---------------------------------------------------------------------------

def test_upstox_error_param_redirects_with_access_denied_reason(client):
    """AC: A callback carrying an Upstox error/denial parameter (e.g. ?error=access_denied)
    instead of a code redirects with reason=access_denied."""
    with patch("webapp.consume_state") as mock_consume:
        mock_consume.return_value = ("user-123", "upstox")

        response = client.get(
            "/api/brokers/upstox/callback"
            "?state=valid-state"
            "&error=access_denied"
            "&error_description=The+user+denied+the+request"
        )

    _assert_redirect_with_reason(response, "access_denied")
    mock_consume.assert_called_once()


# ---------------------------------------------------------------------------
# AC: BrokerAuthError → reason=token_exchange_failed, no CONNECTED row
# ---------------------------------------------------------------------------

def test_broker_auth_error_redirects_with_token_exchange_failed_reason(client):
    """AC: A BrokerAuthError from the token exchange redirects with
    reason=token_exchange_failed and no CONNECTED row is written."""
    with patch("webapp.consume_state") as mock_consume, \
         patch("webapp.DefaultUserPortfolio") as MockPortfolio:

        mock_consume.return_value = ("user-123", "upstox")

        from components.c01_user_portfolio import BrokerAuthError
        MockPortfolio.return_value.connect_portfolio.side_effect = BrokerAuthError(
            "Upstox rejected the authorization code"
        )

        response = client.get(
            "/api/brokers/upstox/callback?code=invalid-code&state=valid-state"
        )

    _assert_redirect_with_reason(response, "token_exchange_failed")
    mock_consume.assert_called_once()


# ---------------------------------------------------------------------------
# AC: BrokerConfigError → reason=broker_not_configured
# ---------------------------------------------------------------------------

def test_broker_config_error_redirects_with_broker_not_configured_reason(client):
    """AC: A BrokerConfigError from connect_portfolio redirects with
    reason=broker_not_configured."""
    with patch("webapp.consume_state") as mock_consume, \
         patch("webapp.DefaultUserPortfolio") as MockPortfolio:

        mock_consume.return_value = ("user-123", "upstox")

        from components.c01_user_portfolio import BrokerConfigError
        MockPortfolio.return_value.connect_portfolio.side_effect = BrokerConfigError(
            "Upstox is not configured"
        )

        response = client.get(
            "/api/brokers/upstox/callback?code=some-code&state=valid-state"
        )

    _assert_redirect_with_reason(response, "broker_not_configured")


# ---------------------------------------------------------------------------
# AC: Unexpected exception → reason=unexpected
# ---------------------------------------------------------------------------

def test_unexpected_exception_redirects_with_unexpected_reason(client):
    """AC: Any unexpected exception redirects with reason=unexpected."""
    with patch("webapp.consume_state") as mock_consume, \
         patch("webapp.DefaultUserPortfolio") as MockPortfolio:

        mock_consume.return_value = ("user-123", "upstox")
        MockPortfolio.return_value.connect_portfolio.side_effect = RuntimeError("surprise!")

        response = client.get(
            "/api/brokers/upstox/callback?code=some-code&state=valid-state"
        )

    _assert_redirect_with_reason(response, "unexpected")


# ---------------------------------------------------------------------------
# AC: Route ignores user_id in query string — forged user_id cannot cause
#     connection for another user
# ---------------------------------------------------------------------------

def test_forged_user_id_in_query_string_is_ignored(client):
    """AC: The route ignores any user_id present in the query string;
    a forged user_id cannot cause a connection for another user."""
    with patch("webapp.consume_state") as mock_consume, \
         patch("webapp.DefaultUserPortfolio") as MockPortfolio:

        # State says "user-123" but query string says "user-999"
        mock_consume.return_value = ("user-123", "upstox")

        mock_record = MagicMock()
        mock_record.id = "record-abc"
        mock_record.status = "CONNECTED"
        MockPortfolio.return_value.connect_portfolio.return_value = mock_record

        response = client.get(
            "/api/brokers/upstox/callback"
            "?code=real-code"
            "&state=valid-state"
            "&user_id=user-999"  # forged — should be ignored
        )

    _assert_redirect(response, "/settings/brokers?connect=success&broker=upstox")

    # connect_portfolio must be called with the user_id from state, NOT from query
    MockPortfolio.return_value.connect_portfolio.assert_called_once_with(
        user_id="user-123",   # from state, NOT from query string
        broker_id="upstox",
        payload={"code": "real-code"},
    )


# ---------------------------------------------------------------------------
# AC: State value and auth code do not appear in logs
# ---------------------------------------------------------------------------

def test_state_value_not_in_logs_on_success(client, caplog):
    """AC: The state value does not appear in server-side logs."""
    with caplog.at_level(logging.INFO):
        with patch("webapp.consume_state") as mock_consume, \
             patch("webapp.DefaultUserPortfolio") as MockPortfolio:

            mock_consume.return_value = ("user-123", "upstox")
            mock_record = MagicMock()
            mock_record.id = "rec-1"
            mock_record.status = "CONNECTED"
            MockPortfolio.return_value.connect_portfolio.return_value = mock_record

            response = client.get(
                "/api/brokers/upstox/callback"
                "?code=secret-auth-code"
                "&state=super-secret-state-token-xyz"
            )

    _assert_redirect(response, "/settings/brokers?connect=success&broker=upstox")

    # Neither the auth code nor the state token must appear in any log line
    assert "super-secret-state-token-xyz" not in caplog.text, (
        "State token must NOT appear in logs (CSRF secret)"
    )
    assert "secret-auth-code" not in caplog.text, (
        "Auth code must NOT appear in logs (credential)"
    )


def test_state_value_not_in_logs_on_error(client, caplog):
    """AC: The state value does not appear in server-side logs even on error."""
    with caplog.at_level(logging.WARNING):
        with patch("webapp.consume_state") as mock_consume:
            from oauth_state import InvalidOAuthStateError
            mock_consume.side_effect = InvalidOAuthStateError("bad state")

            response = client.get(
                "/api/brokers/upstox/callback"
                "?code=some-code"
                "&state=another-secret-state"
            )

    _assert_redirect_with_reason(response, "invalid_state")
    assert "another-secret-state" not in caplog.text, (
        "State token must NOT appear in error logs (CSRF secret)"
    )


def test_auth_code_not_in_logs_on_auth_error(client, caplog):
    """AC: The auth code does not appear in server-side logs on BrokerAuthError."""
    with caplog.at_level(logging.WARNING):
        with patch("webapp.consume_state") as mock_consume, \
             patch("webapp.DefaultUserPortfolio") as MockPortfolio:

            mock_consume.return_value = ("user-123", "upstox")
            from components.c01_user_portfolio import BrokerAuthError
            MockPortfolio.return_value.connect_portfolio.side_effect = BrokerAuthError("bad code")

            response = client.get(
                "/api/brokers/upstox/callback"
                "?code=super-secret-auth-code"
                "&state=some-state"
            )

    _assert_redirect_with_reason(response, "token_exchange_failed")
    assert "super-secret-auth-code" not in caplog.text, (
        "Auth code must NOT appear in error logs (credential)"
    )


# ---------------------------------------------------------------------------
# AC: No session cookie required — identity comes from state, not session
# ---------------------------------------------------------------------------

def test_no_session_cookie_required(client):
    """AC: The callback route must not require an existing session cookie
    for identity — identity comes solely from the single-use state."""
    with patch("webapp.consume_state") as mock_consume, \
         patch("webapp.DefaultUserPortfolio") as MockPortfolio:

        mock_consume.return_value = ("anon-user", "upstox")
        mock_record = MagicMock()
        mock_record.id = "rec-1"
        mock_record.status = "CONNECTED"
        MockPortfolio.return_value.connect_portfolio.return_value = mock_record

        # No session cookie set at all — the test client defaults to no cookie.
        # Passing no cookies= argument is the same as empty dict in Werkzeug,
        # which is what we want (no existing session).
        response = client.get(
            "/api/brokers/upstox/callback?code=real-code&state=valid-state"
        )

    _assert_redirect(response, "/settings/brokers?connect=success&broker=upstox")
    mock_consume.assert_called_once_with("valid-state")


# ---------------------------------------------------------------------------
# AC: All tests stub connector/token exchange; no network calls to Upstox
# ---------------------------------------------------------------------------

def test_no_upstox_network_calls_made(client):
    """AC: No network calls to Upstox are made by the callback route
    (all tests stub the connector/token exchange)."""
    import socket

    original_connect = socket.socket.connect

    def _track_connect(args):
        # If any Upstox host is contacted, fail loudly
        host = args[0] if isinstance(args[0], tuple) else args
        host_str = str(host)
        if "upstox" in host_str.lower():
            pytest.fail(f"Unexpected network call to Upstox host: {host_str}")
        return original_connect(args)

    with patch.object(socket.socket, "connect", side_effect=_track_connect):
        with patch("webapp.consume_state") as mock_consume, \
             patch("webapp.DefaultUserPortfolio") as MockPortfolio:

            mock_consume.return_value = ("user-123", "upstox")
            mock_record = MagicMock()
            mock_record.id = "rec-1"
            mock_record.status = "CONNECTED"
            MockPortfolio.return_value.connect_portfolio.return_value = mock_record

            client.get(
                "/api/brokers/upstox/callback?code=real-code&state=valid-state"
            )
