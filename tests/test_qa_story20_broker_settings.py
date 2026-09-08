"""QA tests for STORY-20 — broker settings UI.

Verifies every acceptance criterion using the Flask test client only;
no network calls to Upstox are made.
"""

from __future__ import annotations

import re
import unittest.mock

import pytest

from webapp import create_app
from components.c01_user_portfolio import (
    StubBrokerConnector,
    register_broker_connector,
    unregister_broker_connector,
)


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
            connector = type(
                "TestUpstoxConnector",
                (),
                {
                    "broker_id": "upstox",
                    "display_name": "Upstox",
                    "_UPSTOX_AUTHORIZE_URL": "https://api.upstox.com/v2/login/authorization/dialog",
                    "_config": config,
                    "_http": unittest.mock.Mock(),
                    "build_authorize_url": lambda self, state: (
                        f"{self._UPSTOX_AUTHORIZE_URL}"
                        f"?response_type=code&client_id={self._config.client_id}"
                        f"&redirect_uri={self._config.redirect_uri}&state={state}"
                    ),
                },
            )()
            register_broker_connector(connector)


# ---------------------------------------------------------------------------
# GET /api/brokers/connections  (no OAuth / no DB needed)
# ---------------------------------------------------------------------------

class TestBrokerConnectionsEndpoint:
    def test_returns_200_with_available_brokers(self):
        app = create_app()
        register_broker_connector(StubBrokerConnector())
        client = _make_authenticated_client(app)

        response = client.get("/api/brokers/connections")

        assert response.status_code == 200
        data = response.get_json()
        assert "available_brokers" in data
        assert isinstance(data["available_brokers"], list)

    def test_includes_broker_id_and_display_name(self):
        app = create_app()
        stub = StubBrokerConnector()
        register_broker_connector(stub)
        client = _make_authenticated_client(app)

        brokers = client.get("/api/brokers/connections").get_json()["available_brokers"]

        assert any(b["broker_id"] == "stub" for b in brokers)
        assert any(b["display_name"] == "Stub Broker" for b in brokers)

    def test_returns_only_the_real_shipped_brokers_when_no_test_broker_registered(self):
        """Upstox is a real, shipped broker (BROKER_CONNECTORS), not a
        test double -- it must always appear here regardless of what a
        test dynamically registers/unregisters, matching the real
        production behavior of a server where nothing ever calls
        register_broker_connector(). Previously list_available_brokers()
        only read the dynamic registry, so a real, unconfigured
        production server always returned []  and the Connect button
        never rendered for anyone; this asserts the fix instead of the
        original bug."""
        app = create_app()
        # Ensure no test-only connector is registered
        unregister_broker_connector("stub")
        client = _make_authenticated_client(app)

        brokers = client.get("/api/brokers/connections").get_json()["available_brokers"]

        assert isinstance(brokers, list)
        assert [b["broker_id"] for b in brokers] == ["upstox"]


# ---------------------------------------------------------------------------
# GET /settings/brokers  (no OAuth / no DB needed)
# ---------------------------------------------------------------------------

class TestSettingsBrokersPage:
    def test_returns_200(self):
        app = create_app()
        register_broker_connector(StubBrokerConnector())
        client = _make_authenticated_client(app)

        response = client.get("/settings/brokers")

        assert response.status_code == 200

    def test_one_button_per_broker_labelled_connect_display_name(self):
        """AC: The screen renders one connect button per entry in available_brokers,
        labelled 'Connect <display_name>'."""
        app = create_app()
        register_broker_connector(StubBrokerConnector())
        client = _make_authenticated_client(app)

        html = client.get("/settings/brokers").get_data(as_text=True)

        # StubBrokerConnector: broker_id='stub', display_name='Stub Broker'
        assert "Connect Stub Broker" in html
        assert 'data-broker-id="stub"' in html

    def test_no_hardcoded_upstox_reference(self):
        """Template renders every 'Connect <broker>' button from the
        `available_brokers` loop variable, never a literal, hardcoded
        broker name in the markup itself -- source-inspected directly
        on the .html file, since Upstox is now a real, always-present
        shipped broker (BROKER_CONNECTORS) and so always legitimately
        appears in a live-rendered page regardless of what's registered
        for this test."""
        from pathlib import Path

        template_path = (
            Path(__file__).resolve().parent.parent
            / "templates"
            / "settings_brokers.html"
        )
        source = template_path.read_text()

        assert "Connect Upstox" not in source
        assert "Connect {{ broker.display_name }}" in source

    def test_button_is_not_disabled_on_page_load(self):
        """AC: Button is disabled and shows a loading state while the request
        is pending — on page load (before any click) the button must NOT be
        disabled."""
        app = create_app()
        register_broker_connector(StubBrokerConnector())
        client = _make_authenticated_client(app)

        html = client.get("/settings/brokers").get_data(as_text=True)

        # Extract the broker-connect-btn markup and verify it has no 'disabled' attr
        btn_match = re.search(
            r'<button\b[^>]*\bclass="[^"]*broker-connect-btn[^"]*"[^>]*>.*?</button>',
            html,
            re.DOTALL,
        )
        assert btn_match is not None, "broker-connect-btn not found in HTML"
        btn_html = btn_match.group(0)
        assert "disabled" not in btn_html, (
            "Button must NOT be disabled before user clicks"
        )

    def test_retry_button_is_present_for_error_recovery(self):
        """AC: A 500/network failure renders a visible inline error with a
        retry control."""
        app = create_app()
        register_broker_connector(StubBrokerConnector())
        client = _make_authenticated_client(app)

        html = client.get("/settings/brokers").get_data(as_text=True)

        assert 'class="btn btn-sm broker-retry-btn"' in html

    def test_broker_not_configured_message_slot_is_hidden_on_page_load(self):
        """The explanatory env-var message is hidden on load (only shown after 503)."""
        app = create_app()
        register_broker_connector(StubBrokerConnector())
        client = _make_authenticated_client(app)

        html = client.get("/settings/brokers").get_data(as_text=True)

        # The message slot should have the 'hidden' class on page load
        assert 'broker-not-configured-msg' in html

    def test_button_has_broker_id_data_attribute_for_dynamic_endpoint_construction(self):
        """AC: Clicking the button POSTs to /api/brokers/<broker_id>/connect.
        The broker_id is read from the button's data-broker-id attribute and
        used to construct the endpoint URL in JS — verify the attribute is correct."""
        app = create_app()
        register_broker_connector(StubBrokerConnector())
        client = _make_authenticated_client(app)

        html = client.get("/settings/brokers").get_data(as_text=True)

        # The button must carry the correct broker_id so JS can construct the endpoint
        assert 'data-broker-id="stub"' in html


# ---------------------------------------------------------------------------
# POST /api/brokers/upstox/connect — needs DB for OAuth state
# ---------------------------------------------------------------------------

class TestUpstoxConnectNotConfigured:
    """AC: 503 broker_not_configured response names the env vars."""

    def test_returns_503_with_broker_not_configured_error(self, postgres_dsn: str):
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        client = _make_authenticated_client(app)

        with unittest.mock.patch("webapp.UpstoxConfig.from_env") as mock_from_env:
            from upstox_config import BrokerConfigError

            mock_from_env.side_effect = BrokerConfigError(
                "Missing or empty Upstox configuration: "
                "UPSTOX_CLIENT_ID, UPSTOX_CLIENT_SECRET, UPSTOX_REDIRECT_URI."
            )
            response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 503
        data = response.get_json()
        assert data["error"] == "broker_not_configured"
        assert "UPSTOX_CLIENT_ID" in data["message"]
        assert "UPSTOX_CLIENT_SECRET" in data["message"]
        assert "UPSTOX_REDIRECT_URI" in data["message"]


class TestUpstoxConnectHappyPath:
    """AC: Happy-path POST returns 200 with authorize_url and state."""

    def test_returns_200_with_authorize_url_and_state(self, postgres_dsn: str):
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

    def test_no_real_http_call_to_upstox(self, postgres_dsn: str):
        """AC: Tests mock the API layer; no real Upstox calls."""
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        _register_real_upstox_connector(app)
        client = _make_authenticated_client(app)

        response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 200
        # The connector's HTTP mock was never invoked — no real network call
        with app.app_context():
            from components.c01_user_portfolio import get_broker_connector

            connector = get_broker_connector("upstox")
            assert not connector._http.called

    def test_unauthenticated_returns_401(self, postgres_dsn: str):
        import oauth_state
        oauth_state.configure(postgres_dsn)

        app = create_app()
        client = app.test_client()  # no session

        response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# STORY-20 new acceptance-criterion tests — JS-driven behaviour
# (these specifically target what the existing suite does NOT cover)
# ---------------------------------------------------------------------------

class TestUpstoxConnectJSBehaviour:
    """Verify the JS frontend renders the server's own error message on 503.

    The AC says the button renders disabled with "the named-env-var explanation".
    The explanation comes from the server (503 body.message) — the JS must render
    that server string, not a different hardcoded one.
    """

    def test_js_shows_server_error_message_not_hardcoded_string_on_503(self):
        """AC: A 503 broker_not_configured response renders the button disabled
        plus the server's named-env-var explanation — verify the JS passes the
        actual response body to showNotConfigured, not a different hardcoded string.

        The server returns:
          {"error": "broker_not_configured",
           "message": "Missing or empty Upstox configuration: UPSTOX_CLIENT_ID, ..."}

        The JS MUST use result.body.message in showNotConfigured, NOT a different
        hardcoded string."""
        app = create_app()
        with app.app_context():
            unregister_broker_connector("stub")
        client = _make_authenticated_client(app)
        html = client.get("/settings/brokers").get_data(as_text=True)

        # Find the 503 broker_not_configured branch inside connectBroker
        branch_start = html.find("result.status === 503")
        assert branch_start != -1, "503 broker_not_configured branch not found in JS"
        branch_end = html.find("return;", branch_start)
        branch = html[branch_start:branch_end]

        # The branch must pass result.body.message (the server's real error) to showNotConfigured
        # NOT a hardcoded string literal
        assert "result.body.message" in branch, (
            "JS must pass result.body.message (server's 503 error message) to showNotConfigured, "
            "not a hardcoded string. The server's message names the specific env vars to set."
        )

    def test_connect_button_uses_fetch_then_full_page_navigate_on_success(self):
        """AC: Clicking the button issues exactly one POST to /api/brokers/<id>/connect
        and then navigates the browser to the returned authorize_url (full-page navigation,
        not fetch/XHR — because Upstox's dialog must be shown to the user)."""
        import re
        app = create_app()
        with app.app_context():
            unregister_broker_connector("stub")
        client = _make_authenticated_client(app)
        html = client.get("/settings/brokers").get_data(as_text=True)

        # Extract the connectBroker function body — find the opening brace
        # then scan forward to the matching closing brace
        func_start = html.find('function connectBroker(brokerId)')
        assert func_start != -1, "connectBroker function not found in JS"
        brace_start = html.find('{', func_start)
        depth = 0
        pos = brace_start
        while pos < len(html):
            c = html[pos]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    break
            pos += 1
        func_body = html[brace_start + 1:pos]

        # The function MUST use fetch (XMLHttpRequest not acceptable)
        assert 'fetch(' in func_body, (
            "connectBroker must use fetch() to POST to the API"
        )

        # The success path must do full-page navigation: window.location.href = ...
        # NOT a fetch-then-react/rerender approach
        assert 'window.location.href' in func_body, (
            "Success path must do full-page navigation (window.location.href), "
            "not XHR/fetch-and-rerender — Upstox dialog requires it"
        )
        assert 'result.body.authorize_url' in func_body, (
            "Navigation must use the authorize_url from the server response"
        )

    def test_loading_state_sets_button_disabled_and_changes_text(self):
        """AC: Button is disabled and shows a loading state while the request is pending;
        double-clicking does not fire two requests."""
        import re
        app = create_app()
        with app.app_context():
            unregister_broker_connector("stub")
        client = _make_authenticated_client(app)
        html = client.get("/settings/brokers").get_data(as_text=True)

        # Extract setLoading function
        func_match = re.search(
            r'function setLoading\(brokerId,\s*loading\)\s*\{(.*?)\n\s*\}',
            html,
            re.DOTALL,
        )
        assert func_match is not None, "setLoading function not found in JS"
        func_body = func_match.group(1)

        # When loading=true: btn.disabled = true must be set
        # (find the loading=true branch)
        loading_true = func_body.split('}')[0]  # rough split to get the true branch
        assert 'btn.disabled' in func_body, (
            "setLoading must set btn.disabled property"
        )

        # The button must be disabled synchronously in setLoading, before the fetch call.
        # Verify that connectBroker calls setLoading(true) BEFORE fetch
        connect_match = re.search(
            r'function connectBroker\(brokerId\)\s*\{(.*?)\n\s*\}\s*;?\s*$',
            html,
            re.DOTALL | re.MULTILINE,
        )
        assert connect_match is not None
        connect_body = connect_match.group(1)

        # setLoading(true) must appear before fetch(
        setloading_idx = connect_body.find('setLoading')
        fetch_idx = connect_body.find('fetch(')
        assert setloading_idx != -1, "connectBroker must call setLoading"
        assert fetch_idx != -1, "connectBroker must call fetch"
        assert setloading_idx < fetch_idx, (
            "setLoading(true) must be called BEFORE fetch() so the button is "
            "disabled synchronously and prevents double-clicks"
        )

    def test_503_check_uses_status_and_error_fields_from_response(self):
        """AC: A 503 broker_not_configured response renders the button disabled
        plus the named-env-var explanation. The JS must check result.status===503
        AND result.body.error==='broker_not_configured', not just status alone."""
        import re
        app = create_app()
        with app.app_context():
            unregister_broker_connector("stub")
        client = _make_authenticated_client(app)
        html = client.get("/settings/brokers").get_data(as_text=True)

        func_match = re.search(
            r'function connectBroker\(brokerId\)\s*\{(.*?)\n\s*\}\s*;?\s*$',
            html,
            re.DOTALL | re.MULTILINE,
        )
        assert func_match is not None
        func_body = func_match.group(1)

        # The 503 broker_not_configured branch must check BOTH status and error field
        # Pattern: if (result.status === 503 && result.body.error === 'broker_not_configured')
        has_status_check = 'result.status' in func_body and '503' in func_body
        has_error_check = "result.body.error" in func_body and "'broker_not_configured'" in func_body
        assert has_status_check and has_error_check, (
            "JS must check both result.status===503 AND "
            "result.body.error==='broker_not_configured' to avoid false positives "
            "on other 5xx errors"
        )
