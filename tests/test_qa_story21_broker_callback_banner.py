"""QA tests for STORY-21 — post-callback success/failure banner on
/settings/brokers.

Like STORY-20's own test file, this covers what a Flask test client and
source inspection can verify for real: the banner markup exists, the
reason-to-message mapping is present and complete, no raw reason/state/
code value is ever interpolated into a displayed string, and the
query-param-stripping / refetch logic is present in the shipped script.

True in-browser behavior (does the banner actually SHOW on
?connect=success, does history.replaceState actually run, is the
dismiss button really focused, does the DOM update on refetch) requires
a real browser and is deferred to a check_live_ui call, exactly as
STORY-20's own test_qa_story20_broker_settings_live_ui.py already
established as this project's real convention for this kind of check.
"""

from __future__ import annotations

import re

import pytest

from webapp import create_app


def _make_authenticated_client(app, user_id="test-user-123"):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    return client


REASON_TO_MESSAGE = {
    "invalid_state": "That connection link was invalid or already used. Please try connecting again.",
    "state_replayed": "That connection link was invalid or already used. Please try connecting again.",
    "state_expired": "The connection request expired. Please try again.",
    "missing_code": "Upstox did not grant access. You can try again.",
    "access_denied": "Upstox did not grant access. You can try again.",
    "token_exchange_failed": "We could not complete the connection with Upstox. Please try again.",
    "broker_not_configured": "This broker is not configured by the administrator. Please contact support.",
}


def _get_settings_brokers_html(app):
    client = _make_authenticated_client(app)
    response = client.get("/settings/brokers")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_banner_markup_present_with_role_alert_and_dismiss_button():
    app = create_app()
    html = _get_settings_brokers_html(app)

    assert 'id="broker-callback-banner"' in html
    assert 'role="alert"' in html
    assert 'id="broker-callback-banner-dismiss"' in html
    assert 'id="broker-callback-banner-text"' in html


def test_import_now_prompt_present_for_story22_to_wire_up():
    app = create_app()
    html = _get_settings_brokers_html(app)
    assert 'id="broker-callback-import-now-link"' in html


def test_every_documented_reason_has_its_specific_message_in_the_script():
    app = create_app()
    html = _get_settings_brokers_html(app)

    for reason, message in REASON_TO_MESSAGE.items():
        assert f"{reason}:" in html, f"missing REASON_MESSAGES entry for {reason!r}"
        assert message in html, f"missing exact message text for {reason!r}"


def test_generic_failure_message_present_for_unrecognised_reasons():
    app = create_app()
    html = _get_settings_brokers_html(app)
    assert "GENERIC_FAILURE_MESSAGE" in html
    assert "Something went wrong connecting your broker. Please try again." in html


def test_success_message_is_the_documented_exact_text():
    app = create_app()
    html = _get_settings_brokers_html(app)
    assert "Upstox connected successfully." in html


def test_query_param_stripping_logic_present():
    app = create_app()
    html = _get_settings_brokers_html(app)

    assert "history.replaceState" in html
    assert "params.delete('connect')" in html
    assert "params.delete('broker')" in html
    assert "params.delete('reason')" in html


def test_refetch_of_connections_endpoint_present_for_success_path():
    app = create_app()
    html = _get_settings_brokers_html(app)
    assert "refetchConnections" in html
    assert "/api/brokers/connections" in html


def test_dismiss_button_receives_focus_when_banner_shown():
    """Keyboard-accessible: the dismiss control is real programmatic
    focus target, not just visually present."""
    app = create_app()
    html = _get_settings_brokers_html(app)
    assert "dismissBtn.focus()" in html


def test_no_raw_reason_slug_or_state_or_code_is_ever_interpolated_into_display_text():
    """The script must map every reason through the fixed
    REASON_MESSAGES table -- never render params.get('reason') (or
    'state'/'code') directly into the banner text."""
    app = create_app()
    html = _get_settings_brokers_html(app)

    # The only direct uses of the raw reason value are as a lookup KEY
    # into REASON_MESSAGES, never concatenated/assigned straight into
    # the displayed text.
    script_match = re.search(r"handlePostCallbackBanner.*?\}\)\(\);", html, re.DOTALL)
    assert script_match, "could not locate the post-callback banner script block"
    script = script_match.group(0)

    assert "REASON_MESSAGES[reason]" in script
    # Never directly assigns the raw reason/state/code param to the
    # banner's displayed text or innerHTML.
    assert "text.textContent = reason" not in script
    assert "text.textContent = params.get" not in script
