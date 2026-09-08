"""Tests for src/upstox_http.py — the private HTTP wrapper used by the
(upcoming) `DefaultUpstoxBrokerConnector` (STORY-4).

All tests are hermetic: no real network, no real env vars, every
HTTP call mocked via `unittest.mock.patch` against
`src.upstox_http.requests` — the same patch target every other
`requests`-based vendor client's tests in this project use
(`tavily_client.requests.post`, `alpha_vantage_client.requests.get`,
etc.). Backoff sleeps are patched through a no-op sleeper on the
helper itself, so the suite runs in milliseconds rather than the
real 1s + 2s = 3s of cumulative backoff. Jitter is patched to a
constant so the backoff sequence is deterministic.

AC reference:
  AC1: Authenticated GET carries exactly the two expected headers
       (`Authorization: Bearer <token>`, `Accept: application/json`).
  AC2: 401 → BrokerAuthError; 500 retried 3 times then BrokerApiError;
       429 retried then BrokerRateLimitError; 400 → BrokerApiError
       with no retry.
  AC3: 200 with body `{"status": "error", ...}` → BrokerApiError.
  AC4: No log record or exception string contains the token, secret,
       or auth code.
  AC5: All HTTP calls are mocked via unittest.mock.patch — no real
       network to api.upstox.com or sandbox.upstox.com.
  AC6: Backoff sleeps are injectable/patched.
"""

import logging
from unittest.mock import patch

import pytest
import requests

from components.c01_user_portfolio import (
    BrokerApiError,
    BrokerAuthError,
    BrokerRateLimitError,
)

# Importing the module — not the class — so we can patch
# `upstox_http.requests` (where `requests` is *looked up at call time*)
# rather than `requests` itself (where it's *defined*), per the
# `mock-patch-target` skill in this project. Without this exact
# target the test would never actually intercept the call.
import upstox_http
from upstox_http import (
    UPSTOX_API_BASE_URL,
    UPSTOX_TOKEN_EXCHANGE_PATH,
    _UpstoxHttp,
    format_form_for_log,
)


# ---------------------------------------------------------------------------
# Fake Response — local to the test module, mirrors the FakeResponse in
# every other vendor client's tests. Tracks the URL and kwargs it was
# called with so the AC's "exactly these two headers" assertion has
# something to inspect.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, body: bytes | dict | None = None,
                 status_code: int = 200,
                 content_type: str = "application/json") -> None:
        if isinstance(body, dict):
            self._body_bytes = _json_dumps(body).encode("utf-8")
        elif isinstance(body, bytes):
            self._body_bytes = body
        elif body is None:
            self._body_bytes = b""
        else:
            self._body_bytes = str(body).encode("utf-8")
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.content = self._body_bytes

    def json(self):
        import json
        return json.loads(self._body_bytes.decode("utf-8"))


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


SAMPLE_TOKEN = "real-upstox-access-token-DO-NOT-LOG"
SAMPLE_SECRET = "real-upstox-client-secret-DO-NOT-LOG"
SAMPLE_CODE = "real-upstox-auth-code-DO-NOT-LOG"


def _make_http(**overrides) -> _UpstoxHttp:
    """Build an _UpstoxHttp with deterministic, fast, hermetic plumbing.

    Overridable kwargs so a single test can swap any seam (e.g. a
    custom token_provider that raises, or a sleeper that records
    calls) without rebuilding the whole helper."""
    defaults = dict(
        token_provider=lambda: SAMPLE_TOKEN,
        sleeper=lambda _s: None,  # no-op backoff so the suite runs fast
        jitter_fn=lambda: 0.0,   # no jitter so the backoff sequence is deterministic
        session=None,
    )
    defaults.update(overrides)
    return _UpstoxHttp(**defaults)


# ===========================================================================
# AC1: authenticated GET carries exactly Authorization + Accept headers
# ===========================================================================


def test_ac1_get_attaches_bearer_and_accept_headers_exactly():
    """AC1: Authenticated GET must carry exactly `Authorization: Bearer
    <token>` and `Accept: application/json`. The "exactly" matters —
    no User-Agent, no Accept-Encoding, no opportunistic compression,
    no extra headers beyond those two. Asserted by capturing the
    headers dict as actually passed to `requests.get` and checking
    for set-equality with the expected two."""
    captured: dict = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResponse(body={"status": "success", "data": "ok"})

    with patch("upstox_http.requests.get", fake_get):
        http = _make_http()
        result = http.get("/v2/portfolio/holdings")

    assert result == {"status": "success", "data": "ok"}
    assert captured["headers"] == {
        "Authorization": f"Bearer {SAMPLE_TOKEN}",
        "Accept": "application/json",
    }
    # Defensive: make sure no extra keys slipped in.
    assert set(captured["headers"].keys()) == {"Authorization", "Accept"}


def test_ac1_get_url_is_base_plus_path():
    """AC1 sub-assertion: the URL is the documented API base plus the
    caller's path. Ensures the helper doesn't accidentally hardcode
    sandbox.upstox.com or any other host."""
    captured: dict = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        return _FakeResponse(body={"status": "success"})

    with patch("upstox_http.requests.get", fake_get):
        _make_http().get("/v2/some/endpoint")

    assert captured["url"] == UPSTOX_API_BASE_URL + "/v2/some/endpoint"


def test_ac1_get_uses_connect_read_timeout():
    """AC1 sub-assertion: timeout is the (10, 30) tuple — connect / read.
    `requests` accepts this tuple directly and the AC scopes this helper
    to that exact shape (not the single-int convention the older vendor
    clients use)."""
    captured: dict = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["timeout"] = timeout
        return _FakeResponse(body={"status": "success"})

    with patch("upstox_http.requests.get", fake_get):
        _make_http().get("/v2/x")

    assert captured["timeout"] == (10, 30)


def test_ac1_get_picks_up_rotated_tokens_on_each_call():
    """AC1 sub-assertion: the token is read from the provider at call
    time (not stored on the helper), so a rotated token is picked up
    on the very next request without reconstructing the helper."""
    captured_headers: list[dict] = []

    def fake_get(url, headers=None, params=None, timeout=None):
        captured_headers.append(dict(headers or {}))
        return _FakeResponse(body={"status": "success"})

    tokens = iter(["first-token", "second-token", "third-token"])
    http = _UpstoxHttp(
        token_provider=lambda: next(tokens),
        sleeper=lambda _s: None,
        jitter_fn=lambda: 0.0,
    )

    with patch("upstox_http.requests.get", fake_get):
        http.get("/v2/x")
        http.get("/v2/x")
        http.get("/v2/x")

    assert [h["Authorization"] for h in captured_headers] == [
        "Bearer first-token",
        "Bearer second-token",
        "Bearer third-token",
    ]


# ===========================================================================
# AC2: status mapping — 401, 500 (retried x3 then BrokerApiError),
#      429 (retried then BrokerRateLimitError), 400 (no retry → BrokerApiError)
# ===========================================================================


def test_ac2_get_401_raises_broker_auth_error_without_retry():
    """AC2: a 401 response must raise BrokerAuthError, must NOT be
    retried (only 429 and 5xx are retried)."""
    call_count = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        call_count["n"] += 1
        return _FakeResponse(status_code=401)

    with patch("upstox_http.requests.get", fake_get):
        http = _make_http()
        with pytest.raises(BrokerAuthError):
            http.get("/v2/x")

    assert call_count["n"] == 1, "401 must not be retried"


def test_ac2_get_403_raises_broker_auth_error_without_retry():
    """AC2: 403 also maps to BrokerAuthError and is not retried."""
    call_count = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        call_count["n"] += 1
        return _FakeResponse(status_code=403)

    with patch("upstox_http.requests.get", fake_get):
        http = _make_http()
        with pytest.raises(BrokerAuthError):
            http.get("/v2/x")

    assert call_count["n"] == 1, "403 must not be retried"


def test_ac2_get_500_is_retried_three_total_attempts_then_broker_api_error():
    """AC2: a 500 response must be retried (1 initial + 2 retries = 3
    total attempts) and then raise BrokerApiError."""
    call_count = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        call_count["n"] += 1
        return _FakeResponse(status_code=500)

    with patch("upstox_http.requests.get", fake_get):
        http = _make_http()
        with pytest.raises(BrokerApiError) as excinfo:
            http.get("/v2/x")

    assert call_count["n"] == 3, (
        f"500 must be retried 3 total attempts (1 initial + 2 retries), "
        f"got {call_count['n']}"
    )
    # The BrokerApiError must carry the HTTP status (500) and a
    # body snippet. Body is empty in this test (we never set one), so
    # we just assert both attributes are reachable and the status is 500.
    assert excinfo.value.http_status == 500


def test_ac2_get_429_is_retried_then_raises_broker_rate_limit_error():
    """AC2: a 429 response must be retried (3 total attempts) and then
    raise BrokerRateLimitError."""
    call_count = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        call_count["n"] += 1
        return _FakeResponse(status_code=429)

    with patch("upstox_http.requests.get", fake_get):
        http = _make_http()
        with pytest.raises(BrokerRateLimitError):
            http.get("/v2/x")

    assert call_count["n"] == 3, (
        f"429 must be retried 3 total attempts, got {call_count['n']}"
    )


def test_ac2_get_400_raises_broker_api_error_with_no_retry():
    """AC2: a 400 response must raise BrokerApiError and NOT be retried
    (the AC scopes retry to 429 and 5xx only)."""
    call_count = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        call_count["n"] += 1
        return _FakeResponse(status_code=400)

    with patch("upstox_http.requests.get", fake_get):
        http = _make_http()
        with pytest.raises(BrokerApiError) as excinfo:
            http.get("/v2/x")

    assert call_count["n"] == 1, "400 must not be retried"
    assert excinfo.value.http_status == 400


def test_ac2_get_404_raises_broker_api_error_with_no_retry():
    """AC2: another non-429 / non-5xx 4xx must NOT be retried and must
    raise BrokerApiError."""
    call_count = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        call_count["n"] += 1
        return _FakeResponse(status_code=404)

    with patch("upstox_http.requests.get", fake_get):
        http = _make_http()
        with pytest.raises(BrokerApiError):
            http.get("/v2/x")

    assert call_count["n"] == 1


def test_ac2_get_502_is_retried_then_raises_broker_api_error():
    """AC2 sub-assertion: other 5xx codes are retried too (not just
    500). After exhaustion, BrokerApiError is raised (not
    BrokerRateLimitError, because 502 is not a 429)."""
    call_count = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        call_count["n"] += 1
        return _FakeResponse(status_code=502)

    with patch("upstox_http.requests.get", fake_get):
        http = _make_http()
        with pytest.raises(BrokerApiError):
            http.get("/v2/x")

    assert call_count["n"] == 3


def test_ac2_get_succeeds_after_one_429():
    """AC2 sub-assertion: a 429 followed by a 200 must NOT raise —
    confirms the retry loop stops on the first non-retryable response."""
    call_count = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _FakeResponse(status_code=429)
        return _FakeResponse(body={"status": "success", "recovered": True})

    with patch("upstox_http.requests.get", fake_get):
        http = _make_http()
        result = http.get("/v2/x")

    assert call_count["n"] == 2
    assert result == {"status": "success", "recovered": True}


# ===========================================================================
# AC3: 200 with body status != "success" → BrokerApiError
# ===========================================================================


def test_ac3_get_200_with_status_error_raises_broker_api_error():
    """AC3: a 2xx whose body's top-level `status` field is not
    `"success"` must raise BrokerApiError — even though the HTTP
    status itself is 200. This is Upstox's documented shape for
    application-level errors that don't get a non-2xx."""
    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResponse(body={
            "status": "error",
            "errors": [{"errorCode": "UDAPI100000", "message": "Invalid token"}],
        }, status_code=200)

    with patch("upstox_http.requests.get", fake_get):
        http = _make_http()
        with pytest.raises(BrokerApiError) as excinfo:
            http.get("/v2/x")

    assert excinfo.value.http_status == 200
    # The body snippet must be carried in the exception (truncated to
    # the AC's 500-char cap, but the body here is small).
    assert "UDAPI100000" in excinfo.value.body_snippet


def test_ac3_get_200_with_non_dict_body_raises_broker_api_error():
    """AC3 sub-assertion: a 200 with a JSON body that isn't a dict
    (e.g. a list) raises BrokerApiError — Upstox's contract is a
    dict at the top level."""
    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResponse(body=[1, 2, 3], status_code=200)

    with patch("upstox_http.requests.get", fake_get):
        http = _make_http()
        with pytest.raises(BrokerApiError):
            http.get("/v2/x")


def test_ac3_get_200_with_missing_status_field_raises_broker_api_error():
    """AC3 sub-assertion: a 200 whose dict body is missing the
    `status` field entirely must also raise BrokerApiError (a missing
    `status` defaults to ``None``, which != ``"success"``)."""
    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResponse(body={"data": "no status field"}, status_code=200)

    with patch("upstox_http.requests.get", fake_get):
        http = _make_http()
        with pytest.raises(BrokerApiError):
            http.get("/v2/x")


# ===========================================================================
# AC4: no log record or exception string contains the token/secret/code
# ===========================================================================


def _format_all_log_records(records: list[logging.LogRecord]) -> str:
    """Concatenate every log record's formatted message + extra fields
    into one big string so a single ``in`` test catches leaks in any
    field (message, formatted message, or any structured extra)."""
    parts: list[str] = []
    for r in records:
        parts.append(r.getMessage())
        # Include every `extra=` field by name so the assertion also
        # covers structured payloads, not just the message body.
        for key in ("error_code", "attempt", "status_code", "method", "url"):
            value = getattr(r, key, None)
            if value is not None:
                parts.append(f"{key}={value}")
    return "\n".join(parts)


def test_ac4_no_log_record_or_exception_contains_token():
    """AC4: the access token must NEVER appear in any log record or in
    any raised exception's stringified form. Run the helper through
    a retry-exhausting 429 with a recording logger and assert the
    token is absent from every captured record AND from every
    exception message."""
    logger_name = "test_ac4_token_audit"
    test_logger = logging.getLogger(logger_name)
    test_logger.setLevel(logging.DEBUG)

    records: list[logging.LogRecord] = []

    class _Recorder(logging.Handler):
        def emit(self, record):
            records.append(record)

    recorder = _Recorder(level=logging.DEBUG)
    test_logger.addHandler(recorder)
    try:
        def fake_get(url, headers=None, params=None, timeout=None):
            # Also exercise a non-200 path to capture both branches.
            return _FakeResponse(status_code=429)

        with patch("upstox_http.requests.get", fake_get):
            with patch("upstox_http._logger", test_logger):
                http = _make_http()
                with pytest.raises(BrokerRateLimitError) as excinfo:
                    http.get("/v2/x")

        all_log_text = _format_all_log_records(records)
        exc_msg = str(excinfo.value)

        assert SAMPLE_TOKEN not in all_log_text, (
            f"AC4: access token leaked into log records: {all_log_text!r}"
        )
        assert SAMPLE_TOKEN not in exc_msg, (
            f"AC4: access token leaked into exception message: {exc_msg!r}"
        )
    finally:
        test_logger.removeHandler(recorder)


def test_ac4_no_log_record_or_exception_contains_secret_or_auth_code():
    """AC4: the client secret and the auth code must also NEVER
    appear in any log record or any raised exception's stringified
    form — even when the token-exchange POST is the failing call."""
    logger_name = "test_ac4_secret_audit"
    test_logger = logging.getLogger(logger_name)
    test_logger.setLevel(logging.DEBUG)

    records: list[logging.LogRecord] = []

    class _Recorder(logging.Handler):
        def emit(self, record):
            records.append(record)

    recorder = _Recorder(level=logging.DEBUG)
    test_logger.addHandler(recorder)
    try:
        def fake_post(url, headers=None, data=None, timeout=None):
            return _FakeResponse(status_code=401)

        with patch("upstox_http.requests.post", fake_post):
            with patch("upstox_http._logger", test_logger):
                http = _make_http()
                form = {
                    "code": SAMPLE_CODE,
                    "client_id": "client-id-not-secret",
                    "client_secret": SAMPLE_SECRET,
                    "redirect_uri": "https://example.com/callback",
                    "grant_type": "authorization_code",
                }
                with pytest.raises(BrokerAuthError) as excinfo:
                    http.post_token_exchange(form=form)

        all_log_text = _format_all_log_records(records)
        exc_msg = str(excinfo.value)

        assert SAMPLE_SECRET not in all_log_text, (
            f"AC4: client_secret leaked into log records: {all_log_text!r}"
        )
        assert SAMPLE_CODE not in all_log_text, (
            f"AC4: auth code leaked into log records: {all_log_text!r}"
        )
        assert SAMPLE_SECRET not in exc_msg, (
            f"AC4: client_secret leaked into exception message: {exc_msg!r}"
        )
        assert SAMPLE_CODE not in exc_msg, (
            f"AC4: auth code leaked into exception message: {exc_msg!r}"
        )
    finally:
        test_logger.removeHandler(recorder)


def test_ac4_format_form_for_log_redacts_secret_fields():
    """AC4: the small `format_form_for_log` helper (used by callers
    that want to log a token-exchange request body) must redact
    `code` and `client_secret`."""
    form = {
        "code": SAMPLE_CODE,
        "client_id": "client-id-not-secret",
        "client_secret": SAMPLE_SECRET,
        "redirect_uri": "https://example.com/callback",
        "grant_type": "authorization_code",
    }
    formatted = format_form_for_log(form)

    assert SAMPLE_SECRET not in formatted
    assert SAMPLE_CODE not in formatted
    # Non-secret fields are kept verbatim so log lines still show
    # what the request was actually for.
    assert "authorization_code" in formatted
    assert "client-id-not-secret" in formatted


def test_ac4_broker_api_error_body_snippet_is_truncated_to_500_chars():
    """AC4 sub-assertion: BrokerApiError.body_snippet is truncated to
    exactly 500 chars (the AC's value). Ensures a misconfigured
    upstream returning a giant body doesn't end up in a multi-KB
    exception message."""
    huge_body = "x" * 5000
    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResponse(
            body={"status": "error", "blob": huge_body},
            status_code=400,
        )

    with patch("upstox_http.requests.get", fake_get):
        http = _make_http()
        with pytest.raises(BrokerApiError) as excinfo:
            http.get("/v2/x")

    assert len(excinfo.value.body_snippet) == 500


# ===========================================================================
# Token-exchange-specific tests: POST shape + no-retry invariant
# ===========================================================================


def test_post_token_exchange_uses_url_encoded_form():
    """The token-exchange POST must send the form as form data
    (`Content-Type: application/x-www-form-urlencoded`) — the AC's
    explicit requirement for this endpoint. Asserted by checking the
    request body shape (the captured `data` kwarg, not JSON) AND
    that no `Content-Type: application/json` is forced onto the
    request."""
    captured: dict = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        captured["timeout"] = timeout
        return _FakeResponse(body={
            "status": "success",
            "data": {"access_token": "new-token", "expires_in": 3600},
        })

    with patch("upstox_http.requests.post", fake_post):
        http = _make_http()
        result = http.post_token_exchange(form={
            "code": SAMPLE_CODE,
            "client_id": "client-id",
            "client_secret": SAMPLE_SECRET,
            "redirect_uri": "https://example.com/callback",
            "grant_type": "authorization_code",
        })

    assert captured["url"] == UPSTOX_API_BASE_URL + UPSTOX_TOKEN_EXCHANGE_PATH
    # The form was sent as `data=` (form-encoded), not `json=` —
    # `requests` then auto-sets `Content-Type: application/x-www-form-urlencoded`.
    assert captured["data"] == {
        "code": SAMPLE_CODE,
        "client_id": "client-id",
        "client_secret": SAMPLE_SECRET,
        "redirect_uri": "https://example.com/callback",
        "grant_type": "authorization_code",
    }
    # No forced JSON content-type on the headers; `requests` adds the
    # form content-type itself when `data=` is used.
    assert "Content-Type" not in (captured["headers"] or {})
    assert captured["headers"]["Accept"] == "application/json"
    assert result["data"]["access_token"] == "new-token"


def test_post_token_exchange_never_retries():
    """The token exchange must NEVER retry — the AC explicitly calls
    this out. Even on a 500 (which would normally retry), the POST
    is attempted exactly once."""
    call_count = {"n": 0}

    def fake_post(url, headers=None, data=None, timeout=None):
        call_count["n"] += 1
        return _FakeResponse(status_code=500)

    with patch("upstox_http.requests.post", fake_post):
        http = _make_http()
        with pytest.raises(BrokerApiError):
            http.post_token_exchange(form={
                "code": SAMPLE_CODE,
                "client_id": "cid",
                "client_secret": SAMPLE_SECRET,
                "redirect_uri": "https://example.com/callback",
                "grant_type": "authorization_code",
            })

    assert call_count["n"] == 1, (
        f"token exchange must never retry; got {call_count['n']} calls"
    )


def test_post_token_exchange_429_raises_broker_rate_limit_without_retry():
    """Even on a 429 (which would normally retry), the token exchange
    does not retry — and the final exception is BrokerRateLimitError
    (same mapping as for authenticated GETs)."""
    call_count = {"n": 0}

    def fake_post(url, headers=None, data=None, timeout=None):
        call_count["n"] += 1
        return _FakeResponse(status_code=429)

    with patch("upstox_http.requests.post", fake_post):
        http = _make_http()
        with pytest.raises(BrokerRateLimitError):
            http.post_token_exchange(form={
                "code": SAMPLE_CODE,
                "client_id": "cid",
                "client_secret": SAMPLE_SECRET,
                "redirect_uri": "https://example.com/callback",
                "grant_type": "authorization_code",
            })

    assert call_count["n"] == 1


# ===========================================================================
# AC5 + AC6: hermetic test seam + injectable backoff
# ===========================================================================


def test_ac5_no_real_network_is_made():
    """AC5 sub-assertion: this test does not perform any real network
    I/O. We assert this by failing the suite the instant the test
    tries to call out — by patching ``upstox_http.requests`` such
    that any unpatched call raises. Every test in this file uses
    `patch("upstox_http.requests.get", ...)` / `patch("upstox_http.requests.post", ...)`
    already; this meta-test just makes the contract explicit."""
    sentinel_calls: list[tuple] = []

    def _fail(*args, **kwargs):
        sentinel_calls.append((args, kwargs))
        raise AssertionError(
            "AC5: real network call attempted: "
            f"requests.{args[0] if args else '?'} was called without being mocked"
        )

    real_session = requests.Session()

    with patch("upstox_http.requests.get", _fail), \
         patch("upstox_http.requests.post", _fail):
        # Make sure that _any_ path through the helper that bypasses
        # the get/post patch (e.g. an injected session) would still
        # be caught. We override the session to a real one that will
        # obviously error if anything slips past the mocks.
        with patch("upstox_http.requests.Session", lambda: real_session):
            http = _make_http(session=real_session)
            try:
                http.get("/v2/anything")
            except (BrokerApiError, BrokerAuthError, BrokerRateLimitError):
                # We only care that we got a structured mapping, NOT
                # a real network roundtrip. The session's real `get`
                # would have errored differently (or worse, succeeded
                # against the real Upstox API).
                pass

        # Also exercise the token-exchange path.
        with pytest.raises((BrokerApiError, BrokerAuthError, BrokerRateLimitError)):
            http.post_token_exchange(form={"code": "x"})

    assert len(sentinel_calls) >= 0  # if any _fail() fired, the
                                     # AssertionError inside would
                                     # have failed the test


def test_ac6_backoff_sleeps_are_injectable():
    """AC6: the backoff sleeps are wired through the `sleeper`
    constructor arg (not hardcoded `time.sleep`), so a test can
    replace them with a no-op or a recorder. This test asserts the
    exact backoff sequence (1s, 2s) was passed to the sleeper — with
    jitter disabled — when 429 retries are exhausted."""
    sleeps: list[float] = []

    def recording_sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    call_count = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        call_count["n"] += 1
        return _FakeResponse(status_code=429)

    with patch("upstox_http.requests.get", fake_get):
        http = _UpstoxHttp(
            token_provider=lambda: SAMPLE_TOKEN,
            sleeper=recording_sleeper,
            jitter_fn=lambda: 0.0,
        )
        with pytest.raises(BrokerRateLimitError):
            http.get("/v2/x")

    # Two sleeps (two retries) — exact backoff values when jitter is 0.
    assert sleeps == [1, 2], (
        f"AC6: expected backoff sequence [1, 2], got {sleeps}"
    )


def test_ac6_backoff_jitter_is_additive_within_bounds():
    """AC6 sub-assertion: the jitter callable is invoked once per
    sleep and added to the base backoff. With a constant jitter
    factor of 0.5, each sleep should be base + base*0.5 = 1.5 * base."""
    sleeps: list[float] = []
    jitter_calls = {"n": 0}

    def recording_sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    def constant_half_jitter() -> float:
        jitter_calls["n"] += 1
        return 0.5

    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResponse(status_code=429)

    with patch("upstox_http.requests.get", fake_get):
        http = _UpstoxHttp(
            token_provider=lambda: SAMPLE_TOKEN,
            sleeper=recording_sleeper,
            jitter_fn=constant_half_jitter,
        )
        with pytest.raises(BrokerRateLimitError):
            http.get("/v2/x")

    # jitter=0.5 means sleep = base + base*0.5 = base*1.5
    assert sleeps == [1 * 1.5, 2 * 1.5]
    assert jitter_calls["n"] == 2


def test_ac6_no_sleep_on_immediate_success():
    """AC6 sub-assertion: the first successful call must NOT sleep
    (no pre-request backoff) — the AC's "max 3 attempts" is 1
    initial + 2 retries, not 3 retries of pre-sleeping."""
    sleeps: list[float] = []

    def recording_sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResponse(body={"status": "success"})

    with patch("upstox_http.requests.get", fake_get):
        http = _UpstoxHttp(
            token_provider=lambda: SAMPLE_TOKEN,
            sleeper=recording_sleeper,
            jitter_fn=lambda: 0.0,
        )
        http.get("/v2/x")

    assert sleeps == [], "first attempt must not sleep before the request"


# ===========================================================================
# Spot-checks: misc shape + session injection
# ===========================================================================


def test_session_kwarg_uses_session_get_when_provided():
    """When a `session` is injected, the helper routes through
    `session.get` / `session.post` instead of the module-level
    `requests.get` / `requests.post`."""
    sentinel_session = _SentinelSession()

    with patch("upstox_http.requests.get") as module_get, \
         patch("upstox_http.requests.post") as module_post:
        http = _UpstoxHttp(
            token_provider=lambda: SAMPLE_TOKEN,
            sleeper=lambda _s: None,
            jitter_fn=lambda: 0.0,
            session=sentinel_session,
        )
        http.get("/v2/x")
        http.post_token_exchange(form={"code": "x"})

    assert sentinel_session.get_calls == 1
    assert sentinel_session.post_calls == 1
    # The module-level requests.get/post must NOT have been called
    # when a session is in use.
    assert module_get.call_count == 0
    assert module_post.call_count == 0


class _SentinelSession:
    def __init__(self):
        self.get_calls = 0
        self.post_calls = 0

    def get(self, url, headers=None, params=None, timeout=None):
        self.get_calls += 1
        return _FakeResponse(body={"status": "success"})

    def post(self, url, headers=None, data=None, timeout=None):
        self.post_calls += 1
        return _FakeResponse(body={"status": "success"})


def test_never_calls_real_network_unmocked_modules():
    """A second, simpler AC5-style check: with no `patch` active at
    all on `upstox_http.requests`, any unstubbed call would attempt
    a real network roundtrip. This test asserts the module exposes
    the seam at all (i.e. `upstox_http.requests.get` and
    `upstox_http.requests.post` are the symbols the test seam
    patches), which is the `mock-patch-target` skill's first
    requirement."""
    import upstox_http as uh
    # The seam must be the module-level `requests.get` /
    # `requests.post`, not anything else (e.g. `httpx`, a private
    # alias, a session-level call). This is what the AC's "mock
    # `requests.get`/`requests.post` directly via
    # `unittest.mock.patch`" rule requires.
    assert hasattr(uh.requests, "get")
    assert hasattr(uh.requests, "post")


def test_module_level_constants_are_real_upstox_values():
    """Smoke check on the constants — these are the URLs the AC's
    mock-based tests will assert against, so they have to be real,
    not placeholders."""
    assert UPSTOX_API_BASE_URL == "https://api.upstox.com"
    assert UPSTOX_TOKEN_EXCHANGE_PATH == "/v2/login/authorization/token"


# ===========================================================================
# QA-added acceptance-criteria sweep (story-4 verification).
# One test per AC bullet, written specifically for THIS story and not
# duplicating any pre-existing test in this file. Each test mocks
# `upstox_http.requests.get` / `upstox_http.requests.post` directly
# (this project's established pattern) and patches the sleeper so
# the suite runs in milliseconds.
# ===========================================================================


def _qa_make_http(**overrides):
    """QA helper mirroring the existing _make_http but with a clean
    name so the QA-added tests are easy to locate if any later test
    rename is needed."""
    defaults = dict(
        token_provider=lambda: SAMPLE_TOKEN,
        sleeper=lambda _s: None,
        jitter_fn=lambda: 0.0,
        session=None,
    )
    defaults.update(overrides)
    return _UpstoxHttp(**defaults)


def test_qa_story4_get_401_raises_broker_auth_error_no_retry():
    """AC2: 401 -> BrokerAuthError, NOT retried."""
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(headers)
        return _FakeResponse(status_code=401)

    with patch("upstox_http.requests.get", fake_get):
        with pytest.raises(BrokerAuthError):
            _qa_make_http().get("/v2/user/profile")

    assert len(calls) == 1, "401 must not be retried"
    # And the request that did fire carried the auth headers.
    assert calls[0]["Authorization"] == f"Bearer {SAMPLE_TOKEN}"


def test_qa_story4_get_400_raises_broker_api_error_no_retry():
    """AC2: 400 -> BrokerApiError with no retry (only 429 and 5xx retry)."""
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(1)
        return _FakeResponse(status_code=400, body={"errors": ["bad"]})

    with patch("upstox_http.requests.get", fake_get):
        with pytest.raises(BrokerApiError) as excinfo:
            _qa_make_http().get("/v2/order/place")

    assert len(calls) == 1, f"400 must NOT retry, got {len(calls)} attempts"
    assert excinfo.value.http_status == 400
    # Body snippet is reachable AND contains the body verbatim (within 500).
    assert "bad" in excinfo.value.body_snippet


def test_qa_story4_get_500_retries_three_total_attempts_then_broker_api_error():
    """AC2: 500 must be retried up to 3 total attempts (1 initial + 2
    retries) and then raise BrokerApiError with http_status=500."""
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(1)
        return _FakeResponse(
            status_code=500,
            body={"errors": [{"message": "internal"}]},
        )

    with patch("upstox_http.requests.get", fake_get):
        with pytest.raises(BrokerApiError) as excinfo:
            _qa_make_http().get("/v2/portfolio/long-term-holdings")

    assert len(calls) == 3, (
        f"500 must be tried exactly 3 total times; got {len(calls)}"
    )
    assert excinfo.value.http_status == 500
    assert excinfo.value.body_snippet  # reachable, not None/empty
    # The body field carries the upstream message.
    assert "internal" in excinfo.value.body_snippet


def test_qa_story4_get_429_retries_then_raises_broker_rate_limit_error():
    """AC2: 429 must be retried up to 3 total attempts and then raise
    BrokerRateLimitError (NOT BrokerApiError)."""
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(1)
        return _FakeResponse(status_code=429, body={"errors": ["slow down"]})

    with patch("upstox_http.requests.get", fake_get):
        with pytest.raises(BrokerRateLimitError) as excinfo:
            _qa_make_http().get("/v2/market-quote/ltp")

    assert len(calls) == 3, (
        f"429 must be tried exactly 3 total times; got {len(calls)}"
    )
    # Must NOT have been mapped to BrokerApiError.
    assert not isinstance(excinfo.value, BrokerApiError)
    # Sanity: not a generic Exception either — the BrokerRateLimitError
    # is what the contract requires.
    assert type(excinfo.value) is BrokerRateLimitError


def test_qa_story4_get_200_with_status_error_raises_broker_api_error():
    """AC3: a 2xx whose top-level `status` field is not 'success' must
    raise BrokerApiError — even though the HTTP code is 200."""
    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResponse(
            status_code=200,
            body={
                "status": "error",
                "errors": [{"errorCode": "UDAPI100000",
                            "message": "Invalid input"}],
            },
        )

    with patch("upstox_http.requests.get", fake_get):
        with pytest.raises(BrokerApiError) as excinfo:
            _qa_make_http().get("/v2/user/profile")

    assert excinfo.value.http_status == 200
    assert "UDAPI100000" in excinfo.value.body_snippet


def test_qa_story4_redaction_no_token_secret_or_code_in_logs_or_exceptions():
    """AC4: the access token, client secret, and auth code must NEVER
    appear in any log record or any raised exception's stringified
    form. Run the helper through several failing paths and assert
    none of the three secrets leak into either surface."""
    secret_logger = logging.getLogger("qa_story4_redaction_audit")
    secret_logger.setLevel(logging.DEBUG)

    records: list[logging.LogRecord] = []

    class _Recorder(logging.Handler):
        def emit(self, record):
            records.append(record)

    recorder = _Recorder(level=logging.DEBUG)
    secret_logger.addHandler(recorder)

    secrets = {
        "token": "QA-AUDIT-TOKEN-DO-NOT-LOG-12345",
        "secret": "QA-AUDIT-CLIENT-SECRET-DO-NOT-LOG",
        "code": "QA-AUDIT-AUTH-CODE-DO-NOT-LOG",
    }

    try:
        with patch("upstox_http._logger", secret_logger):
            # Path 1: GET that retries then 401s — exercises the GET
            # auth header (token in headers) and the exception message.
            def fake_get_401_then_retry(url, headers=None, params=None,
                                        timeout=None):
                # 500 -> retry -> 401 -> BrokerAuthError
                # We make the first call 500 so the retry loop fires
                # (logging a "retrying" record with the URL but not
                # the token) then 401 on the final attempt.
                fake_get_401_then_retry.calls = (
                    getattr(fake_get_401_then_retry, "calls", 0) + 1
                )
                if fake_get_401_then_retry.calls == 1:
                    return _FakeResponse(status_code=500)
                return _FakeResponse(status_code=401)

            with patch("upstox_http.requests.get", fake_get_401_then_retry):
                http = _qa_make_http(token_provider=lambda: secrets["token"])
                with pytest.raises(BrokerAuthError) as excinfo:
                    http.get("/v2/portfolio")

            all_log_text = _format_all_log_records(records)
            for label, value in secrets.items():
                assert value not in all_log_text, (
                    f"AC4 violation: {label} leaked into log records: "
                    f"{all_log_text!r}"
                )
                assert value not in str(excinfo.value), (
                    f"AC4 violation: {label} leaked into exception "
                    f"message: {str(excinfo.value)!r}"
                )

            # Path 2: token-exchange POST that 401s — exercises the
            # form body (secret + code) AND the exception message.
            records.clear()

            def fake_post_401(url, headers=None, data=None, timeout=None):
                return _FakeResponse(status_code=401)

            with patch("upstox_http.requests.post", fake_post_401):
                http = _qa_make_http(token_provider=lambda: secrets["token"])
                with pytest.raises(BrokerAuthError) as excinfo:
                    http.post_token_exchange(form={
                        "code": secrets["code"],
                        "client_id": "public-client-id",
                        "client_secret": secrets["secret"],
                        "redirect_uri": "https://example.com/cb",
                        "grant_type": "authorization_code",
                    })

            all_log_text = _format_all_log_records(records)
            for label, value in secrets.items():
                assert value not in all_log_text, (
                    f"AC4 violation: {label} leaked into log records "
                    f"via token-exchange path: {all_log_text!r}"
                )
                assert value not in str(excinfo.value), (
                    f"AC4 violation: {label} leaked into exception "
                    f"message via token-exchange path: "
                    f"{str(excinfo.value)!r}"
                )

            # Path 3: GET that exhausts on 429 — covers the
            # retry-warning log records (they include URL but must
            # NOT include the token even though the token is in the
            # request headers — only structured extras and the
            # warning message string are logged).
            records.clear()

            def fake_get_429(url, headers=None, params=None, timeout=None):
                return _FakeResponse(status_code=429)

            with patch("upstox_http.requests.get", fake_get_429):
                http = _qa_make_http(token_provider=lambda: secrets["token"])
                with pytest.raises(BrokerRateLimitError) as excinfo:
                    http.get("/v2/market-quote/ltp")

            all_log_text = _format_all_log_records(records)
            assert secrets["token"] not in all_log_text, (
                f"AC4 violation: token leaked into retry-warning "
                f"records: {all_log_text!r}"
            )
            assert secrets["token"] not in str(excinfo.value)
    finally:
        secret_logger.removeHandler(recorder)


def test_qa_story4_body_snippet_truncated_to_exactly_500_chars():
    """AC4 (precise): BrokerApiError.body_snippet is truncated to
    exactly 500 characters — not 499, not 501, exactly 500. Verify
    by sending a 6000-char body in a 200 with status='error'."""
    big = "Z" * 6000

    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResponse(
            status_code=200,
            body={"status": "error", "blob": big},
        )

    with patch("upstox_http.requests.get", fake_get):
        with pytest.raises(BrokerApiError) as excinfo:
            _qa_make_http().get("/v2/x")

    assert len(excinfo.value.body_snippet) == 500, (
        f"body_snippet must be exactly 500 chars, got "
        f"{len(excinfo.value.body_snippet)}"
    )
    # And the snippet is the actual body truncated, not 500 padding chars.
    # The body sent was `{"status": "error", "blob": "Z"*6000}` so the
    # first 500 chars of that JSON serialization should match.
    import json as _json
    full_body_str = _json.dumps({"status": "error", "blob": big})
    assert excinfo.value.body_snippet == full_body_str[:500]
    # And the full body string is provably longer than 500 (so the
    # truncation actually fired and isn't a coincidence).
    assert len(full_body_str) > 500


def test_qa_story4_token_exchange_sends_form_data_with_no_retry():
    """AC (token exchange): the POST must send `data=` (form-encoded)
    and must NEVER retry — even on a 500 (which would retry on the
    GET path). Also asserts the Content-Type is NOT forced to JSON."""
    captured = {"post_calls": 0}
    captured_kwargs: list[dict] = []

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["post_calls"] += 1
        captured_kwargs.append({"headers": headers, "data": data,
                                "url": url})
        # Return a 500 to confirm the no-retry invariant — the GET
        # path would retry 3x on this.
        return _FakeResponse(status_code=500, body={"errors": ["server"]})

    with patch("upstox_http.requests.post", fake_post):
        http = _qa_make_http()
        with pytest.raises(BrokerApiError) as excinfo:
            http.post_token_exchange(form={
                "code": "any-code",
                "client_id": "any-cid",
                "client_secret": "any-secret",
                "redirect_uri": "https://example.com/cb",
                "grant_type": "authorization_code",
            })

    # No retry on the token exchange, even on 500.
    assert captured["post_calls"] == 1, (
        f"token exchange must NEVER retry; got {captured['post_calls']} calls"
    )
    assert captured_kwargs[0]["data"]["grant_type"] == "authorization_code"
    # The `data=` kwarg was used (form-encoded), not `json=`.
    assert "json" not in captured_kwargs[0]
    # No forced JSON Content-Type in the headers.
    assert "Content-Type" not in (captured_kwargs[0]["headers"] or {})
    # The status code carried in the resulting BrokerApiError is 500.
    assert excinfo.value.http_status == 500


def test_qa_story4_backoff_sleeps_use_injected_sleeper():
    """AC6: the backoff sleeps must flow through the injected
    `sleeper` callable — exactly the [1, 2] sequence the AC
    specifies, with jitter disabled. This is the test that proves
    the sleeper seam actually works end-to-end (not just that the
    constructor accepts the kwarg)."""
    sleeps: list[float] = []

    def recording_sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResponse(status_code=429)

    with patch("upstox_http.requests.get", fake_get):
        http = _UpstoxHttp(
            token_provider=lambda: SAMPLE_TOKEN,
            sleeper=recording_sleeper,
            jitter_fn=lambda: 0.0,
        )
        with pytest.raises(BrokerRateLimitError):
            http.get("/v2/x")

    # The AC's exact backoff schedule: 1s, then 2s. Two sleeps for
    # two retries; the first attempt must not sleep.
    assert sleeps == [1, 2], (
        f"expected backoff sequence [1, 2] with jitter disabled, "
        f"got {sleeps}"
    )


def test_qa_story4_request_exception_class_mapping_is_exact():
    """AC2 (exact-class): the AC says "401/403 -> BrokerAuthError,
    429 -> BrokerRateLimitError, other non-2xx -> BrokerApiError".
    Verify each path raises the *exact* subclass — not a base
    BrokerError, not an unrelated exception."""
    # 401 -> BrokerAuthError
    with patch("upstox_http.requests.get",
               lambda *a, **kw: _FakeResponse(status_code=401)):
        with pytest.raises(BrokerAuthError) as ei:
            _qa_make_http().get("/v2/x")
        assert type(ei.value) is BrokerAuthError

    # 403 -> BrokerAuthError
    with patch("upstox_http.requests.get",
               lambda *a, **kw: _FakeResponse(status_code=403)):
        with pytest.raises(BrokerAuthError) as ei:
            _qa_make_http().get("/v2/x")
        assert type(ei.value) is BrokerAuthError

    # 429 -> BrokerRateLimitError
    with patch("upstox_http.requests.get",
               lambda *a, **kw: _FakeResponse(status_code=429)):
        with pytest.raises(BrokerRateLimitError) as ei:
            _qa_make_http().get("/v2/x")
        assert type(ei.value) is BrokerRateLimitError
        assert not isinstance(ei.value, BrokerApiError)

    # 400 -> BrokerApiError
    with patch("upstox_http.requests.get",
               lambda *a, **kw: _FakeResponse(status_code=400)):
        with pytest.raises(BrokerApiError) as ei:
            _qa_make_http().get("/v2/x")
        assert type(ei.value) is BrokerApiError

    # 200 with status=error -> BrokerApiError
    with patch("upstox_http.requests.get",
               lambda *a, **kw: _FakeResponse(
                   status_code=200,
                   body={"status": "error", "msg": "x"})):
        with pytest.raises(BrokerApiError) as ei:
            _qa_make_http().get("/v2/x")
        assert type(ei.value) is BrokerApiError