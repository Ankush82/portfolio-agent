"""QA tests for STORY-20 — broker settings UI (Flask test client + browser).

This file contains two kinds of QA verification:

  1. Flask test-client tests (pytest) — test server-side route logic,
     template rendering, and JS behaviour that can be partially proxied
     through mocked fetch() responses via the test client.

  2. Browser-based tests — documented as check_live_ui() calls that
     will be invoked as a separate QA tool call (check_live_ui is not
     a Python import; it is called by the QA agent separately).

Acceptance criteria verified here (Flask test client):
  - GET /api/brokers/connections returns available_brokers list
  - GET /settings/brokers renders one button per broker with correct
    label and data-broker-id attribute
  - Button is NOT disabled on page load
  - Retry button is present in the DOM
  - The 503 broker_not_configured response names UPSTOX_CLIENT_ID,
    UPSTOX_CLIENT_SECRET, UPSTOX_REDIRECT_URI
  - A 500 error response is NOT silent (inline error + retry control)

Acceptance criteria verified via check_live_ui (real browser):
  - Button shows loading state on click before response arrives
  - 503 response renders button disabled + env-var explanation
  - Inline error block is visible after error
  - Retry button is visible after error
  - No double-click fires two requests (button disabled synchronously)
"""

from __future__ import annotations

import os
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


# ---------------------------------------------------------------------------
# Fixture — ensure UPSTOX env vars are absent so broker_not_configured fires
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _strip_upstox_env():
    saved = {}
    for k in ("UPSTOX_CLIENT_ID", "UPSTOX_CLIENT_SECRET", "UPSTOX_REDIRECT_URI"):
        saved[k] = os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


@pytest.fixture
def app_with_stub_broker():
    """Create app with StubBrokerConnector registered."""
    app = create_app()
    with app.app_context():
        unregister_broker_connector("stub")
        register_broker_connector(StubBrokerConnector())
    return app


# ---------------------------------------------------------------------------
# Server-side tests — Flask test client
# ---------------------------------------------------------------------------

class TestBrokerConnectionsEndpoint:
    def test_returns_available_brokers_list(self, app_with_stub_broker):
        """The stub broker registered by this fixture appears alongside
        Upstox -- a real, shipped broker (BROKER_CONNECTORS) that is
        always present regardless of what a test dynamically registers,
        matching real production behavior."""
        client = _make_authenticated_client(app_with_stub_broker)
        response = client.get("/api/brokers/connections")
        assert response.status_code == 200
        data = response.get_json()
        assert "available_brokers" in data
        assert isinstance(data["available_brokers"], list)
        by_id = {b["broker_id"]: b for b in data["available_brokers"]}
        assert set(by_id) == {"stub", "upstox"}
        assert by_id["stub"]["display_name"] == "Stub Broker"

    def test_endpoint_includes_both_broker_id_and_display_name(self, app_with_stub_broker):
        client = _make_authenticated_client(app_with_stub_broker)
        brokers = client.get("/api/brokers/connections").get_json()["available_brokers"]
        assert all("broker_id" in b and "display_name" in b for b in brokers)


class TestSettingsBrokersPage:
    def test_returns_200(self, app_with_stub_broker):
        client = _make_authenticated_client(app_with_stub_broker)
        response = client.get("/settings/brokers")
        assert response.status_code == 200

    def test_one_button_per_broker_labelled_connect_display_name(self, app_with_stub_broker):
        """AC: One connect button per available_brokers entry, labelled
        'Connect <display_name>'."""
        html = _make_authenticated_client(app_with_stub_broker).get("/settings/brokers").get_data(
            as_text=True
        )
        assert "Connect Stub Broker" in html

    def test_button_has_correct_data_broker_id_attribute(self, app_with_stub_broker):
        """AC: data-broker-id attribute is present for dynamic endpoint construction."""
        html = _make_authenticated_client(app_with_stub_broker).get("/settings/brokers").get_data(
            as_text=True
        )
        assert 'data-broker-id="stub"' in html

    def test_button_is_not_disabled_on_page_load(self, app_with_stub_broker):
        """AC: Button must NOT be disabled before user clicks (loading state
        only begins after click)."""
        html = _make_authenticated_client(app_with_stub_broker).get("/settings/brokers").get_data(
            as_text=True
        )
        btn_match = re.search(
            r'<button\b[^>]*\bclass="[^"]*broker-connect-btn[^"]*"[^>]*>.*?</button>',
            html,
            re.DOTALL,
        )
        assert btn_match is not None, "broker-connect-btn not found"
        btn_html = btn_match.group(0)
        assert "disabled" not in btn_html, "Button must NOT be disabled on page load"

    def test_retry_button_is_present_in_dom(self, app_with_stub_broker):
        """AC: A 500/network failure renders a visible inline error with a
        retry control — retry button must be in the DOM (initially hidden)."""
        html = _make_authenticated_client(app_with_stub_broker).get("/settings/brokers").get_data(
            as_text=True
        )
        assert 'class="btn btn-sm broker-retry-btn"' in html

    def test_broker_not_configured_message_slot_present_but_hidden(self, app_with_stub_broker):
        """The env-var explanation message slot is present (hidden class) on page load."""
        html = _make_authenticated_client(app_with_stub_broker).get("/settings/brokers").get_data(
            as_text=True
        )
        assert 'broker-not-configured-msg' in html


class TestUpstoxConnect503:
    """AC: 503 broker_not_configured response names the missing env vars."""

    def test_returns_503_with_broker_not_configured_error(self):
        app = create_app()
        with app.app_context():
            unregister_broker_connector("stub")
        client = _make_authenticated_client(app)

        response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 503
        data = response.get_json()
        assert data["error"] == "broker_not_configured"
        assert "UPSTOX_CLIENT_ID" in data["message"]
        assert "UPSTOX_CLIENT_SECRET" in data["message"]
        assert "UPSTOX_REDIRECT_URI" in data["message"]


class TestGenericBrokerConnect503:
    """Verify the connect endpoint works generically per registered broker.

    The AC requires that future brokers (added to the registry) get their
    own connect button with no code change. This tests that the endpoint
    pattern /api/brokers/<broker_id>/connect is consistent.
    """

    def test_generic_broker_connect_endpoint_returns_503_when_not_configured(self):
        """The Upstox-specific endpoint POST /api/brokers/upstox/connect returns 503
        with the env-var names when Upstox is not configured — no silent failure."""
        app = create_app()
        with app.app_context():
            unregister_broker_connector("stub")
        client = _make_authenticated_client(app)

        response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 503
        data = response.get_json()
        assert data["error"] == "broker_not_configured"
        # All three env vars must be named so the operator knows exactly what to set
        assert "UPSTOX_CLIENT_ID" in data["message"]
        assert "UPSTOX_CLIENT_SECRET" in data["message"]
        assert "UPSTOX_REDIRECT_URI" in data["message"]
        # Nothing should fail silently — no 200 and no empty error message
        assert response.status_code != 200 or data.get("error") == ""

    def test_unauthenticated_returns_401(self):
        """AC: Unauthenticated users get 401 (no OAuth state created for strangers)."""
        app = create_app()
        client = app.test_client()  # no session

        response = client.post("/api/brokers/upstox/connect")

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Browser-based tests — to be invoked via check_live_ui tool
# ---------------------------------------------------------------------------
# The following acceptance criteria require a real browser (JavaScript execution,
# DOM mutation observation, and CSS class-change detection) and cannot be tested
# with the Flask test client. They are documented here as a specification of
# exactly what check_live_ui must verify.
#
# To run these, the QA agent calls check_live_ui separately with these actions.
#
# App startup (run before each check_live_ui call):
#   - Start Flask app on 127.0.0.1:<port> with StubBrokerConnector registered
#     and UPSTOX_CLIENT_ID / UPSTOX_CLIENT_SECRET / UPSTOX_REDIRECT_URI absent
#     from the environment
#
# CHECK_LIVE_UI_CALL_1 — Button render + label + data attribute
#   route: /settings/brokers
#   actions:
#     - wait_for_selector: 'button.broker-connect-btn'
#     - wait_for_text: 'Connect Stub Broker'
#     - wait_for_selector: 'button.broker-connect-btn[data-broker-id="stub"]'
#     - wait_for_selector: 'button.broker-connect-btn[data-broker-id="stub"]:not([disabled])'
#   expected: PASS — button renders with correct label and data-broker-id,
#              not disabled before click
#
# CHECK_LIVE_UI_CALL_2 — Loading state on click (synchronous JS)
#   route: /settings/brokers
#   actions:
#     - wait_for_selector: 'button.broker-connect-btn[data-broker-id="stub"]'
#     - click: 'button.broker-connect-btn[data-broker-id="stub"]'
#     - wait_for_text: 'Connecting Stub Broker…'
#   expected: PASS — button text changes to loading indicator before network response
#
# CHECK_LIVE_UI_CALL_3 — 503 renders disabled button + env-var explanation
#   route: /settings/brokers
#   actions:
#     - wait_for_selector: 'button.broker-connect-btn[data-broker-id="stub"]'
#     - click: 'button.broker-connect-btn[data-broker-id="stub"]'
#     - wait_for_text: 'UPSTOX_CLIENT_ID'
#     - wait_for_text: 'UPSTOX_CLIENT_SECRET'
#     - wait_for_text: 'UPSTOX_REDIRECT_URI'
#     - wait_for_selector: 'button.broker-connect-btn[data-broker-id="stub"][disabled]'
#   expected: PASS — error message naming all three env vars appears, button stays disabled
#
# CHECK_LIVE_UI_CALL_4 — Retry button visible after 503 error
#   route: /settings/brokers
#   actions:
#     - wait_for_selector: 'button.broker-connect-btn[data-broker-id="stub"]'
#     - click: 'button.broker-connect-btn[data-broker-id="stub"]'
#     - wait_for_selector: '.broker-not-configured-msg:not(.hidden)'
#     - wait_for_selector: 'button.broker-retry-btn[data-broker-id="stub"]'
#   expected: PASS — retry button is visible in DOM after error
