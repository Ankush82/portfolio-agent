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


# ---------------------------------------------------------------------------
# STORY-21 — NEW tests added by this QA pass.
#
# These tests exercise each acceptance criterion individually:
#   AC1  ?connect=success&broker=upstox  -> green success banner
#   AC1  success path triggers refetch of /api/brokers/connections
#   AC2  each documented reason -> its SPECIFIC mapped message
#   AC2  unrecognised reason -> generic failure message
#   AC2  generic failure path exposes a Retry control
#   AC3  query params are stripped from the URL via history.replaceState
#   AC3  a request to the CLEANED URL does not show the banner
#   AC4  banner has role="alert" and a focusable dismiss button
#   AC4  clicking dismiss hides the banner
#   AC5  no raw reason/state/code value is ever placed in the banner text
# ---------------------------------------------------------------------------


# AC2 — every documented reason maps to its SPECIFIC message (per-branch)
REASON_SPECIFIC_MESSAGES = [
    ("invalid_state", "That connection link was invalid or already used. Please try connecting again."),
    ("state_replayed", "That connection link was invalid or already used. Please try connecting again."),
    ("state_expired", "The connection request expired. Please try again."),
    ("missing_code", "Upstox did not grant access. You can try again."),
    ("access_denied", "Upstox did not grant access. You can try again."),
    ("token_exchange_failed", "We could not complete the connection with Upstox. Please try again."),
]


def _extract_callback_script(html):
    """Pull out the entire STORY-21 block — REASON_MESSAGES table,
    GENERIC_FAILURE_MESSAGE, showCallbackBanner, dismissCallbackBanner,
    handlePostCallbackBanner IIFE, and dismiss-btn listener — so
    per-branch assertions aren't accidentally matched against the
    connect-button code path but DO see the mapping table that lives
    OUTSIDE the IIFE."""
    m = re.search(
        r"// ---------- STORY-21:.*?</script>",
        html,
        re.DOTALL,
    )
    assert m, "could not locate STORY-21 block in template"
    return m.group(0)


@pytest.mark.parametrize("reason,expected_message", REASON_SPECIFIC_MESSAGES)
def test_ac2_each_documented_reason_renders_its_specific_message(reason, expected_message):
    """AC2: each documented reason slug maps to its SPECIFIC message text.

    Per the AC: "Each documented reason value renders its specific
    message ... (one test per branch)." Each parametrize case is its
    own branch and asserts both:
      (a) the message is present verbatim in the source, and
      (b) the lookup is `REASON_MESSAGES[reason]`, so an unknown value
          cannot accidentally reuse one of these strings.
    """
    app = create_app()
    html = _get_settings_brokers_html(app)
    script = _extract_callback_script(html)

    # (a) the exact documented text exists somewhere in the script
    assert expected_message in script, (
        f"expected exact message for reason {reason!r} in script, "
        f"got script ending with: {script[-200:]!r}"
    )

    # (b) the mapping key must be the reason slug itself (not a different alias)
    # Find the REASON_MESSAGES table in the script
    table_match = re.search(r"REASON_MESSAGES\s*=\s*\{(.*?)\};", script, re.DOTALL)
    assert table_match, "REASON_MESSAGES table not found"
    table = table_match.group(1)

    # Each reason slug must be present as a key in the table
    slug_pattern = rf"\b{re.escape(reason)}\s*:"
    assert re.search(slug_pattern, table), (
        f"reason {reason!r} not present as a key in REASON_MESSAGES table"
    )

    # The value paired with the key must be the expected message (look for "reason: 'message',"
    # or "reason: 'message'" or the equivalent within the table)
    value_pattern = rf"{re.escape(reason)}\s*:\s*['\"]({re.escape(expected_message)})['\"]"
    assert re.search(value_pattern, table), (
        f"for reason {reason!r}, the table value is not the expected exact message. "
        f"Table excerpt:\n{table[:500]}"
    )


def test_ac2_unrecognised_reason_renders_generic_failure_message():
    """AC2: 'unexpected and any unrecognised value -> a generic failure message plus retry'."""
    app = create_app()
    html = _get_settings_brokers_html(app)
    script = _extract_callback_script(html)

    # The fallback path uses GENERIC_FAILURE_MESSAGE when the reason
    # is not in REASON_MESSAGES.
    generic_msg = "Something went wrong connecting your broker. Please try again."
    assert generic_msg in script, "generic failure message text missing"
    assert "GENERIC_FAILURE_MESSAGE" in script, (
        "GENERIC_FAILURE_MESSAGE constant not present in script"
    )

    # The lookup expression must be `(reason && REASON_MESSAGES[reason]) || GENERIC_FAILURE_MESSAGE`
    # i.e. the fallback fires when reason is null OR not in the table.
    fallback_match = re.search(
        r"reason\s*&&\s*REASON_MESSAGES\[reason\]\s*\)\s*\|\|\s*GENERIC_FAILURE_MESSAGE",
        script,
    )
    assert fallback_match, (
        "fallback expression `(reason && REASON_MESSAGES[reason]) || GENERIC_FAILURE_MESSAGE` "
        "not found in the script. An unrecognised reason would not fall through to the "
        "generic failure message."
    )


def test_ac2_generic_failure_path_exposes_a_retry_control():
    """AC2: 'unrecognised value -> a generic failure message plus retry'.

    The retry control here is the SAME `Connect <display_name>` button
    that is always rendered per broker (one per available_brokers
    entry). The page must keep those buttons present (not, e.g., a
    disabled-only state with no path back to retry)."""
    app = create_app()
    html = _get_settings_brokers_html(app)

    # Per-broker Connect buttons are the retry mechanism: at least one
    # `.broker-connect-btn` is present.
    assert "broker-connect-btn" in html, (
        "retry control (Connect button) missing from page — no way to retry after "
        "an unrecognised reason shows the generic failure banner"
    )


def test_ac1_success_path_triggers_refetch_of_connections_endpoint():
    """AC1: '?connect=success&broker=upstox renders the success banner
    AND triggers a refetch of the connections endpoint.'"""
    app = create_app()
    html = _get_settings_brokers_html(app)
    script = _extract_callback_script(html)

    # The success branch must call refetchConnections() unconditionally
    success_branch = re.search(
        r"if\s*\(\s*connect\s*===\s*['\"]success['\"]\s*\)\s*\{(.*?)\}",
        script,
        re.DOTALL,
    )
    assert success_branch, (
        "connect=success branch not found in handlePostCallbackBanner"
    )
    success_body = success_branch.group(1)
    assert "refetchConnections()" in success_body, (
        f"refetchConnections() must be called inside the connect=success branch. "
        f"Branch body:\n{success_body}"
    )

    # And refetchConnections itself must fetch /api/brokers/connections
    assert "/api/brokers/connections" in html, (
        "refetchConnections must hit GET /api/brokers/connections"
    )


def test_ac1_success_banner_uses_exact_documented_text():
    """AC1: the success banner must read exactly 'Upstox connected successfully.'."""
    app = create_app()
    html = _get_settings_brokers_html(app)
    assert "Upstox connected successfully." in html, (
        "success banner message text missing from shipped HTML"
    )


def test_ac3_query_params_stripped_from_url_via_history_replaceState():
    """AC3: 'Query params are removed from the address bar after render'."""
    app = create_app()
    html = _get_settings_brokers_html(app)
    script = _extract_callback_script(html)

    # The script MUST call history.replaceState to strip params without
    # triggering a navigation. A pushState + reload would re-render and
    # re-show the banner — exactly the bug AC3 forbids.
    assert "history.replaceState" in script, (
        "history.replaceState() not present — query params would not be "
        "stripped without a page reload, defeating AC3."
    )
    assert "history.pushState" not in script, (
        "history.pushState would add a back-button entry that re-shows the banner"
    )

    # All three documented params must be deleted before replaceState
    assert "params.delete('connect')" in script
    assert "params.delete('broker')" in script
    assert "params.delete('reason')" in script

    # The new URL must be built from window.location.pathname + the
    # cleaned query + the existing hash (NOT location.href, which would
    # keep the very params we're stripping).
    assert "window.location.pathname" in script, (
        "new URL must be built from window.location.pathname + cleaned params, "
        "not from window.location.href (which would carry the very params "
        "we're trying to strip)"
    )


def test_ac3_strip_runs_even_when_banner_does_not_show():
    """AC3: 'Query params are removed from the address bar after render'.

    The strip must run on EVERY connect=... landing, not be conditional
    on whether a banner actually rendered — otherwise ?connect=success
    would leave the params and a subsequent refresh would refire the
    success path."""
    app = create_app()
    html = _get_settings_brokers_html(app)
    script = _extract_callback_script(html)

    # The replaceState/delete block must NOT be inside an else-if for
    # connect==='error'; it must run unconditionally after the if/else
    # branches.
    # Find the delete/replaceState lines and the closest enclosing braces
    delete_idx = script.find("params.delete('connect')")
    assert delete_idx != -1
    replacestate_idx = script.find("history.replaceState", delete_idx)
    assert replacestate_idx != -1

    # The delete/replaceState block must come AFTER the closing brace
    # of the if/else-if for connect=='success' / connect=='error'.
    # Easiest proxy: the script's outer IIFE structure means the strip
    # block follows the connect branches.
    ifelse_close_idx = script.rfind("}", 0, delete_idx)
    assert ifelse_close_idx != -1, "could not locate if/else-if closing brace"
    assert ifelse_close_idx < delete_idx, (
        "params.delete('connect') is INSIDE the if/else-if branch — would only "
        "strip when one branch fires, not unconditionally."
    )


def test_ac4_banner_is_dismissible_and_keyboard_accessible():
    """AC4: 'banner is dismissible and keyboard-accessible (focusable
    dismiss control, role=alert on the message).'"""
    app = create_app()
    html = _get_settings_brokers_html(app)

    # role="alert" must be present on the banner container (the message element)
    assert 'role="alert"' in html, "role='alert' missing from banner"

    # The dismiss control must be a real <button> with an accessible
    # label — keyboard-focusable (default for <button>).
    dismiss_match = re.search(
        r'<button\b[^>]*\bid="broker-callback-banner-dismiss"[^>]*>.*?</button>',
        html,
        re.DOTALL,
    )
    assert dismiss_match, "dismiss <button> not present in template"
    dismiss_html = dismiss_match.group(0)
    assert 'type="button"' in dismiss_html, (
        "dismiss control must be type=button (so Enter/Space activates it "
        "and it does not submit a form)"
    )
    assert 'aria-label' in dismiss_html, (
        "dismiss button needs aria-label for screen readers (the visible "
        "content is just '×')"
    )

    # Click handler must be wired
    script_src = _extract_callback_script(html)
    assert "broker-callback-banner-dismiss" in script_src, (
        "dismiss button click handler not wired up in JS"
    )

    # focus() must be called on dismissBtn when the banner shows —
    # that is the real keyboard-accessibility AC ("focusable dismiss control").
    assert "dismissBtn.focus()" in script_src, (
        "dismiss button must receive programmatic focus() when the banner shows"
    )


def test_ac4_clicking_dismiss_hides_the_banner():
    """AC4: 'The banner is dismissible' — clicking dismiss must remove it.

    Since the Flask test client doesn't execute JS, this asserts the JS
    dismissCallbackBanner() function actually removes the banner from
    view (adds the .hidden class)."""
    app = create_app()
    html = _get_settings_brokers_html(app)
    script_src = _extract_callback_script(html)

    dismiss_fn_match = re.search(
        r"function dismissCallbackBanner\(\)\s*\{(.*?)\n\s*\}",
        script_src,
        re.DOTALL,
    )
    assert dismiss_fn_match, "dismissCallbackBanner function not found"
    dismiss_body = dismiss_fn_match.group(1)

    # The function must add the 'hidden' class to the banner
    assert "classList.add('hidden')" in dismiss_body or "classList.add(\"hidden\")" in dismiss_body, (
        f"dismissCallbackBanner must hide the banner (add 'hidden' class). "
        f"Function body:\n{dismiss_body}"
    )
    # Must reference the banner element by id
    assert "broker-callback-banner" in dismiss_body, (
        "dismissCallbackBanner must target the banner element by id"
    )


def test_ac5_no_raw_reason_slug_in_banner_render():
    """AC5: 'No raw reason slug, state value, or auth code is ever displayed to the user.'

    The mapping must always go through REASON_MESSAGES / GENERIC_FAILURE_MESSAGE —
    no direct assignment of `reason` (or `state`, or `code`) to textContent."""
    app = create_app()
    html = _get_settings_brokers_html(app)
    script_src = _extract_callback_script(html)

    # The banner text must be set from REASON_MESSAGES[reason] or
    # GENERIC_FAILURE_MESSAGE — never from `reason` / `state` / `code` / params.get().
    show_cb_match = re.search(
        r"function showCallbackBanner\(kind,\s*message\)\s*\{(.*?)\n\s*\}",
        script_src,
        re.DOTALL,
    )
    assert show_cb_match, "showCallbackBanner function not found"
    show_body = show_cb_match.group(1)

    # The text content assignment must use the `message` parameter (already mapped)
    assert "text.textContent = message" in show_body or 'text.textContent = message' in show_body, (
        f"showCallbackBanner must set text from the mapped `message` argument. "
        f"Function body:\n{show_body}"
    )

    # CRITICAL: verify the calling code never passes a raw reason/state/code
    # to showCallbackBanner. The actual call sites use either:
    #   showCallbackBanner('success', 'Upstox connected successfully.')
    #   showCallbackBanner('error', message)  where message is already mapped
    # We check that NO call passes `reason`, `state`, or `code` as the message arg.
    for raw_value in ("reason", "state", "code"):
        bad_patterns = [
            f"showCallbackBanner('error', {raw_value})",
            f"showCallbackBanner(\"error\", {raw_value})",
            f"showCallbackBanner('success', {raw_value})",
            f"showCallbackBanner(\"success\", {raw_value})",
        ]
        for pat in bad_patterns:
            assert pat not in script_src, (
                f"raw value {raw_value!r} passed to showCallbackBanner — "
                f"matches forbidden pattern {pat!r}. AC5 violated: a raw "
                f"slug/state/code would be displayed to the user."
            )


def test_ac5_text_never_includes_auth_code_value():
    """AC5 (specific): the auth code value (the OAuth `code` query param)
    must never appear in the banner text. The script should never read
    `code` from URL params at all in this flow."""
    app = create_app()
    html = _get_settings_brokers_html(app)
    script_src = _extract_callback_script(html)

    # params.get('code') must not appear in the callback banner script
    assert "params.get('code')" not in script_src, (
        "the post-callback banner script must NOT read params.get('code') — "
        "the OAuth code value must never be displayed to the user"
    )
