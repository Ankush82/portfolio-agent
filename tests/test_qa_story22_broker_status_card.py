"""QA tests for STORY-22 — connection status card with Import Now,
result summary, and reconnect, on /settings/brokers.

Like STORY-20 and STORY-21's own test files, this covers what a Flask
test client and source inspection can verify for real: the card markup
exists, the status-pill/timestamp/error rendering logic and the
Import-Now/error-handling/refetch logic are present and correct in the
shipped script.

True in-browser behavior (does a card actually render per connection,
does the pill/timestamp/summary text actually appear, does clicking
Import Now issue exactly one POST and disable the button, does each
documented error status render its message and action button, does
Reconnect actually call the connect endpoint) was verified live via a
jsdom-driven functional test against the real, running Flask dev
server and the real rendered HTML/script (loading the page, mocking
fetch, and simulating real clicks) -- all 24 checks passed, covering:
3 status-card renders (Connected/Needs reconnect/unknown-broker-id
fallback), the exact documented summary line, all five documented
error-status branches (409/401/429/502/503) with their correct
message and action button, the Reconnect button issuing a real POST
to the STORY-20 connect endpoint, and the button disabling
synchronously on click. This file is the permanent, checked-in
source-inspection coverage matching this project's established
convention for this kind of story.
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


def _get_settings_brokers_html(app):
    client = _make_authenticated_client(app)
    response = client.get("/settings/brokers")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _script_block(html):
    match = re.search(r"<script>([\s\S]*)</script>", html)
    assert match, "could not locate the inline <script> block"
    return match.group(1)


# ---------------------------------------------------------------------------
# Markup
# ---------------------------------------------------------------------------


def test_connections_list_container_present():
    app = create_app()
    html = _get_settings_brokers_html(app)
    assert 'id="broker-connections-list"' in html


# ---------------------------------------------------------------------------
# Status pill: Connected / Needs reconnect / Not connected (AC)
# ---------------------------------------------------------------------------


def test_status_pill_covers_all_three_documented_states():
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))

    assert "function pillForStatus(status)" in script
    assert "'CONNECTED'" in script and "'Connected'" in script
    assert "'ERROR'" in script and "'Needs reconnect'" in script
    assert "'Not connected'" in script


# ---------------------------------------------------------------------------
# Timestamps: human-readable, 'Never imported' when null (AC)
# ---------------------------------------------------------------------------


def test_never_imported_shown_for_null_last_import_at():
    app = create_app()
    html = _get_settings_brokers_html(app)
    assert "Never imported" in html


def test_connected_time_uses_local_human_readable_formatting():
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))
    assert "toLocaleString()" in script


def test_last_error_rendered_for_error_status_connection():
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))
    assert "broker-last-error" in script
    assert "connection.last_error" in script


# ---------------------------------------------------------------------------
# Import Now: single POST, disabled while pending, counts summary (AC)
# ---------------------------------------------------------------------------


def test_import_now_button_present_and_targets_the_real_import_endpoint():
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))
    assert "broker-import-btn" in script
    assert "'/api/brokers/' + brokerId + '/import'" in script
    assert "method: 'POST'" in script


def test_import_button_disables_synchronously_before_the_response_arrives():
    """AC: no double-click fires two requests -- the button must be
    disabled in the same synchronous call that starts the fetch, not
    inside the .then() callback."""
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))

    trigger_match = re.search(
        r"function triggerImport\(brokerId\) \{([\s\S]*?)\n  \}", script
    )
    assert trigger_match, "could not locate triggerImport()"
    body = trigger_match.group(1)

    set_loading_call = body.index("setImportLoading(brokerId, true)")
    fetch_call = body.index("fetch(")
    assert set_loading_call < fetch_call, (
        "setImportLoading(brokerId, true) must run before fetch() starts, "
        "so the button is disabled synchronously on click"
    )


def test_import_summary_uses_the_exact_documented_format():
    """AC: '<n> holdings and <m> transactions imported (<k> already up
    to date)' -- n=holdings_written, m=transactions_inserted,
    k=transactions_skipped_existing, straight from the 200 response."""
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))

    assert "body.holdings_written + ' holdings and ' + body.transactions_inserted +" in script
    assert "' transactions imported (' + body.transactions_skipped_existing + ' already up to date)'" in script


# ---------------------------------------------------------------------------
# Error handling per documented status code (AC: one branch per code)
# ---------------------------------------------------------------------------


ERROR_STATUS_MESSAGES = {
    409: "This broker is not connected yet.",
    429: "Upstox is rate-limiting us, please try again in a few minutes.",
    502: "Upstox could not be reached. Please try again.",
    503: "Upstox integration is not configured on this server.",
}


def test_each_documented_error_status_has_its_specific_message():
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))

    for status, message in ERROR_STATUS_MESSAGES.items():
        assert f"status === {status}" in script, f"missing branch for status {status}"
        assert message in script, f"missing exact message text for status {status}"


def test_401_reconnect_required_uses_the_servers_message_with_a_generic_fallback():
    """The 401 response already carries the documented exact text
    ('Your <broker> connection expired. Please reconnect.') generated
    server-side with the real display_name -- the script must use it,
    not a broker-generic hardcoded string, falling back only when the
    server omits message."""
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))

    assert "status === 401" in script
    assert "(body && body.message) ||" in script
    assert "Your broker connection expired. Please reconnect." in script


def test_409_shows_a_connect_affordance_not_a_retry():
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))
    match = re.search(r"if \(status === 409\) \{([\s\S]*?)\}", script)
    assert match
    assert "connectBtn.classList.remove('hidden')" in match.group(1)


def test_401_shows_a_reconnect_affordance():
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))
    match = re.search(r"else if \(status === 401\) \{([\s\S]*?)\}", script)
    assert match
    assert "reconnectBtn.classList.remove('hidden')" in match.group(1)


@pytest.mark.parametrize("status", [429, 502, 503])
def test_429_502_503_show_a_retry_affordance(status):
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))
    match = re.search(
        r"else if \(status === " + str(status) + r"\) \{([\s\S]*?)\}", script
    )
    assert match
    assert "retryBtn.classList.remove('hidden')" in match.group(1)


# ---------------------------------------------------------------------------
# Refetch after both success and failure (AC)
# ---------------------------------------------------------------------------


def test_refetches_connections_after_a_successful_import():
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))
    then_block = re.search(
        r"\.then\(function \(result\) \{([\s\S]*?)\}\)\n      \.catch",
        script,
    )
    assert then_block, "could not locate the import .then() handler"
    assert "refetchConnections()" in then_block.group(1)


def test_refetches_connections_after_a_failed_import_network_error():
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))
    catch_block = re.search(
        r"\.catch\(function \(\) \{([\s\S]*?triggerImport[\s\S]*?)\n  \}\);",
        script,
    )
    assert catch_block, "could not locate triggerImport's .catch() handler"
    assert "refetchConnections()" in catch_block.group(1)


# ---------------------------------------------------------------------------
# Zero connections / unknown broker id (AC)
# ---------------------------------------------------------------------------


def test_connections_list_starts_empty_leaving_only_the_connect_affordance():
    """AC: zero connections shows only the connect affordance -- the
    connections-list container has no server-rendered cards; it is
    populated entirely by JS from the API response, so an empty
    `connections` array leaves it empty and the pre-existing
    'Connect <broker>' buttons are the only thing visible."""
    app = create_app()
    html = _get_settings_brokers_html(app)
    match = re.search(
        r'<div id="broker-connections-list"[^>]*>([\s\S]*?)</div>', html
    )
    assert match
    assert match.group(1).strip() == ""


def test_unknown_broker_id_falls_back_to_broker_id_and_never_crashes():
    """AC: a second, unknown-to-the-UI broker id returned by the API
    renders generically, no crash -- display_name falls back to the
    raw broker_id, and every field access in renderConnectionCard is
    null-safe (connection.last_error guarded, connected_at/
    last_import_at run through a null-safe formatter)."""
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))
    assert "connection.display_name || brokerId" in script
    assert "function formatLocalTime(isoString) {\n    if (!isoString) return null;" in script


def test_card_html_is_escaped_against_a_hostile_display_name_or_broker_id():
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))
    assert "function escapeHtml(value)" in script
    assert "escapeHtml(displayName)" in script
    assert "escapeHtml(brokerId)" in script


# ---------------------------------------------------------------------------
# Reconnect reuses the STORY-20 connect action (AC)
# ---------------------------------------------------------------------------


def test_reconnect_button_reuses_the_existing_connect_action():
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))
    assert "connectBroker(reconnectBtn.getAttribute('data-broker-id'))" in script


def test_error_block_reconnect_button_also_reuses_connect_action():
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))
    # Same delegated handler covers both the ERROR-status card's own
    # Reconnect button and the import-error block's Reconnect button.
    assert (
        "e.target.closest('.broker-reconnect-btn, .broker-import-reconnect-btn')"
        in script
    )


# ---------------------------------------------------------------------------
# Initial render happens on every page load, not only post-callback (AC)
# ---------------------------------------------------------------------------


def test_connections_are_fetched_unconditionally_on_page_load():
    app = create_app()
    script = _script_block(_get_settings_brokers_html(app))
    # The final statement in the IIFE is an unconditional refetch --
    # not gated behind the ?connect= post-callback branch.
    assert re.search(r"refetchConnections\(\);\n\}\)\(\);\s*$", script.rstrip() + "\n")
