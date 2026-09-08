"""QA tests for STORY-6 — DefaultUpstoxBrokerConnector.exchange_auth_code.

Every test in this file exercises one of the STORY-6 acceptance
criteria verbatim against ``DefaultUpstoxBrokerConnector`` with a
hand-authored ``UpstoxConfig`` and a ``Mock`` standing in for
``_UpstoxHttp`` (the helper is the seam — the connector's
``exchange_auth_code`` calls ``self._http.post_token_exchange`` and
nothing else on the helper). No network is ever contacted, and no
module-level ``requests`` patching is needed because the helper
itself is mocked wholesale.
"""

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, call

import pytest

from components.c01_user_portfolio import (
    BrokerApiError,
    BrokerAuthError,
    BrokerCredentials,
    BrokerRateLimitError,
    DefaultUpstoxBrokerConnector,
)
from upstox_config import UpstoxConfig


# ---------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------

# Verbatim config matching the STORY-5 / STORY-6 QA fixtures, so the
# form-body assertions below can pin the exact client_id and
# redirect_uri in addition to the literal key order.
_CONFIG = UpstoxConfig(
    client_id="story6-client-id",
    client_secret="story6-client-secret",
    redirect_uri="https://example.com/cb",
)

# A valid-looking response carrying every field the fetched docs
# promise — used to exercise the "all fields present" branch.
_FULL_RESPONSE = {
    "access_token": "real-upstox-access-token-abc123",
    "user_id": "UPSTOX-USER-42",
    "expires_in": 3600,
    "refresh_token": "real-upstox-refresh-token-xyz",
    "extra_field_not_promised_by_docs": "kept-in-raw",
}


def _connector(http_mock: Mock) -> DefaultUpstoxBrokerConnector:
    """Construct a connector with the QA-fixture config and the
    caller-supplied ``http_mock`` standing in for ``_UpstoxHttp``.
    No real network, no real ``_UpstoxHttp``."""
    return DefaultUpstoxBrokerConnector(config=_CONFIG, http=http_mock)


# ---------------------------------------------------------------------
# AC: Request is a POST to
# ``https://api.upstox.com/v2/login/authorization/token`` with a
# form-encoded body containing exactly the five documented keys and
# ``grant_type=authorization_code``.
# ---------------------------------------------------------------------


def test_exchange_auth_code_calls_post_token_exchange_with_exact_form_payload():
    """The form passed to ``_UpstoxHttp.post_token_exchange`` is
    exactly the five documented keys, in the order the fetched docs
    list them: ``code``, ``client_id``, ``client_secret``,
    ``redirect_uri``, ``grant_type``. ``grant_type`` is the literal
    ``authorization_code``. No extras (no ``scope``, no PKCE), no
    missing keys — the AC's "exactly the five documented keys"
    rule."""
    http_mock = Mock()
    http_mock.post_token_exchange.return_value = _FULL_RESPONSE

    connector = _connector(http_mock)
    connector.exchange_auth_code(code="real-upstox-auth-code")

    # Exactly one call to the helper, with exactly the documented form.
    assert http_mock.post_token_exchange.call_count == 1
    args, kwargs = http_mock.post_token_exchange.call_args
    assert args == ()
    assert kwargs.keys() == {"form"}
    form = kwargs["form"]
    assert list(form.keys()) == [
        "code",
        "client_id",
        "client_secret",
        "redirect_uri",
        "grant_type",
    ]
    assert form["code"] == "real-upstox-auth-code"
    assert form["client_id"] == _CONFIG.client_id
    assert form["client_secret"] == _CONFIG.client_secret
    assert form["redirect_uri"] == _CONFIG.redirect_uri
    assert form["grant_type"] == "authorization_code"


def test_exchange_auth_code_uses_the_documented_token_exchange_endpoint():
    """The token-exchange endpoint is fixed at
    ``/v2/login/authorization/token`` on ``api.upstox.com`` — verified
    here by reading the helper's own constants rather than calling
    Upstox. The connector is supposed to delegate this URL entirely
    to ``_UpstoxHttp.post_token_exchange``; if a future change ever
    inlined the URL into the connector (or pointed at a different
    path), that regression would surface here."""
    from upstox_http import (
        UPSTOX_API_BASE_URL,
        UPSTOX_TOKEN_EXCHANGE_PATH,
    )

    assert UPSTOX_API_BASE_URL == "https://api.upstox.com"
    assert UPSTOX_TOKEN_EXCHANGE_PATH == "/v2/login/authorization/token"
    assert (UPSTOX_API_BASE_URL + UPSTOX_TOKEN_EXCHANGE_PATH) == (
        "https://api.upstox.com/v2/login/authorization/token"
    )


# ---------------------------------------------------------------------
# AC: A fixture response containing only ``{"access_token": "..."}``
# yields ``BrokerCredentials`` with that token, ``token_type='Bearer'``,
# ``expires_at=None``, ``refresh_token=None``, ``broker_user_id=None``
# — no exception.
# ---------------------------------------------------------------------


def test_exchange_auth_code_minimal_response_yields_bearer_credentials_with_no_extras():
    """Minimal fixture: ``access_token`` is the only field the docs
    promise. Every other field is ``None`` (or the dataclass
    default), and no exception is raised — the AC's
    "missing optional fields is normal, not an error" rule."""
    http_mock = Mock()
    http_mock.post_token_exchange.return_value = {
        "access_token": "minimal-token",
    }

    connector = _connector(http_mock)
    creds = connector.exchange_auth_code(code="some-code")

    assert isinstance(creds, BrokerCredentials)
    assert creds.access_token == "minimal-token"
    assert creds.token_type == "Bearer"
    assert creds.expires_at is None
    assert creds.refresh_token is None
    assert creds.broker_user_id is None


# ---------------------------------------------------------------------
# AC: A fixture response additionally containing ``user_id`` and
# ``expires_in`` populates ``broker_user_id`` and an ``expires_at``
# computed as ``now + expires_in`` seconds.
# ---------------------------------------------------------------------


def test_exchange_auth_code_with_user_id_and_expires_in_populates_both():
    """``user_id`` is mapped to ``broker_user_id`` verbatim;
    ``expires_in`` (seconds) is added to ``datetime.now(timezone.utc)``
    to produce ``expires_at``. Captured ``before`` / ``after`` so the
    floating-point drift between two wall-clock reads doesn't flake
    the test."""
    http_mock = Mock()
    http_mock.post_token_exchange.return_value = {
        "access_token": "full-token",
        "user_id": "UPSTOX-USER-42",
        "expires_in": 3600,
    }

    connector = _connector(http_mock)
    before = datetime.now(timezone.utc)
    creds = connector.exchange_auth_code(code="some-code")
    after = datetime.now(timezone.utc)

    assert creds.broker_user_id == "UPSTOX-USER-42"
    assert isinstance(creds.expires_at, datetime)
    # ``expires_at`` is in ``[before + 3600s, after + 3600s]`` — the
    # window the wall clock could have moved during the call.
    assert before + timedelta(seconds=3600) <= creds.expires_at
    assert creds.expires_at <= after + timedelta(seconds=3600)


def test_exchange_auth_code_with_refresh_token_populates_it():
    """``refresh_token`` is ``None`` unless the field is present —
    and is mapped verbatim when it is."""
    http_mock = Mock()
    http_mock.post_token_exchange.return_value = {
        "access_token": "full-token",
        "refresh_token": "refresh-xyz",
    }

    connector = _connector(http_mock)
    creds = connector.exchange_auth_code(code="some-code")

    assert creds.refresh_token == "refresh-xyz"


def test_exchange_auth_code_absent_optional_fields_default_to_none():
    """Every optional field (``user_id``, ``expires_in``,
    ``refresh_token``) defaults to ``None`` when the response
    doesn't carry it. A second, fully-populated fixture is asserted
    alongside so a regression that drops one of the optional
    fields silently passes one test but not the other."""
    http_mock = Mock()
    http_mock.post_token_exchange.return_value = {
        "access_token": "minimal-token",
    }
    creds_minimal = _connector(http_mock).exchange_auth_code(code="x")
    assert creds_minimal.broker_user_id is None
    assert creds_minimal.expires_at is None
    assert creds_minimal.refresh_token is None

    http_mock2 = Mock()
    http_mock2.post_token_exchange.return_value = _FULL_RESPONSE
    creds_full = _connector(http_mock2).exchange_auth_code(code="x")
    assert creds_full.broker_user_id == "UPSTOX-USER-42"
    assert creds_full.expires_at is not None
    assert creds_full.refresh_token == "real-upstox-refresh-token-xyz"


# ---------------------------------------------------------------------
# AC: A 2xx response with no ``access_token`` raises ``BrokerApiError``.
# ---------------------------------------------------------------------


def test_exchange_auth_code_2xx_without_access_token_raises_broker_api_error():
    """The fetched docs promise ``access_token`` on success; a
    2xx response without it is an upstream-side contract violation
    and surfaces as ``BrokerApiError`` — not as a successful
    ``BrokerCredentials`` with an empty token (a much worse
    failure mode that would let a fake empty token through to the
    caller)."""
    http_mock = Mock()
    http_mock.post_token_exchange.return_value = {"status": "success"}

    connector = _connector(http_mock)
    with pytest.raises(BrokerApiError) as excinfo:
        connector.exchange_auth_code(code="some-code")
    # The message identifies the missing field so an operator
    # tracing the failure can find the upstream contract change.
    assert "access_token" in str(excinfo.value)


# ---------------------------------------------------------------------
# AC: A 400/401 response raises ``BrokerAuthError`` whose message
# instructs the user to reconnect.
# ---------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [400, 401])
def test_exchange_auth_code_4xx_raises_broker_auth_error_with_reconnect_message(
    status_code: int,
):
    """A 4xx from the token endpoint is Upstox's rejection of the
    one-time auth code. The only correct user action is to restart
    the OAuth round-trip, so the exception message says so
    explicitly — the AC's "instructs the user to reconnect" rule.

    429 is deliberately NOT included here: a 429 is rate-limiting,
    not a rejection of the code itself, and surfacing it as
    ``BrokerRateLimitError`` lets the caller retry on a different
    cadence without burning the user's auth code.
    """
    api_exc = BrokerApiError(f"Upstox returned HTTP {status_code}")
    api_exc.http_status = status_code

    http_mock = Mock()
    http_mock.post_token_exchange.side_effect = api_exc

    connector = _connector(http_mock)
    with pytest.raises(BrokerAuthError) as excinfo:
        connector.exchange_auth_code(code="some-code")
    msg = str(excinfo.value).lower()
    assert "reconnect" in msg or "restart" in msg or "connect" in msg, (
        f"BrokerAuthError message must instruct the user to restart "
        f"the connect flow; got: {excinfo.value!r}"
    )


# ---------------------------------------------------------------------
# AC: The token exchange is never retried, even on 5xx.
# ---------------------------------------------------------------------


def test_exchange_auth_code_5xx_raises_broker_api_error_without_retry():
    """A 5xx on the token endpoint surfaces as ``BrokerApiError``
    and the helper is called exactly once — a retry on a token
    exchange would either waste the one-time auth code on a
    duplicate call or trigger Upstox's own duplicate-grant
    rejection, both of which is worse than surfacing the first
    response verbatim. The "no retry" property is enforced by
    ``_UpstoxHttp.post_token_exchange`` (which bypasses the retry
    loop entirely); this test verifies the connector goes through
    that exact code path and does not silently add its own retry
    on top."""
    api_exc = BrokerApiError("Upstox returned HTTP 500")
    api_exc.http_status = 500

    http_mock = Mock()
    http_mock.post_token_exchange.side_effect = api_exc

    connector = _connector(http_mock)
    with pytest.raises(BrokerApiError):
        connector.exchange_auth_code(code="some-code")

    assert http_mock.post_token_exchange.call_count == 1


def test_exchange_auth_code_429_surfaces_as_broker_rate_limit_error():
    """A 429 is rate-limiting, distinct from "rejection of the
    code" (400/401/403/404), so the connector lets
    ``BrokerRateLimitError`` propagate unchanged — the caller can
    retry on a different cadence without burning the user's auth
    code."""
    rl_exc = BrokerRateLimitError("Upstox rate-limited")

    http_mock = Mock()
    http_mock.post_token_exchange.side_effect = rl_exc

    connector = _connector(http_mock)
    with pytest.raises(BrokerRateLimitError):
        connector.exchange_auth_code(code="some-code")


# ---------------------------------------------------------------------
# AC: The auth code and client secret appear in no log output or
# exception message.
# ---------------------------------------------------------------------


def _capture_logs() -> list[str]:
    """Capture log records emitted under ``upstox_http`` and the
    connector's own logger. Returns a list of formatted messages
    ready for substring assertions."""
    captured: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(self.format(record))

    handler = _ListHandler()
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)
    logger = logging.getLogger("components.c01_user_portfolio")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return captured


def test_exchange_auth_code_does_not_leak_auth_code_or_secret_in_logs():
    """The AC's redaction rule. The ``code`` and ``client_secret``
    values the connector passes into the helper are NEVER supposed
    to surface in any log line that this method (or its failure
    path) emits. The helper's own redaction is verified by the
    existing ``test_upstox_http.py`` suite; this test verifies the
    connector layer doesn't undo that by logging the form itself,
    stringifying an exception that contains the form, or otherwise
    leaking the secrets."""
    secret_code = "SECRET-AUTH-CODE-DO-NOT-LOG"
    secret = "SECRET-CLIENT-SECRET-DO-NOT-LOG"

    # A 4xx-style error whose message is derived purely from the
    # HTTP status (no body), so any leakage in the test's log
    # output must come from the connector's own code path, not
    # from the helper's redacted body snippet.
    api_exc = BrokerApiError("Upstox returned HTTP 400")
    api_exc.http_status = 400

    http_mock = Mock()
    http_mock.post_token_exchange.side_effect = api_exc

    captured_before = _capture_logs()

    config = UpstoxConfig(
        client_id="harmless-client-id",
        client_secret=secret,
        redirect_uri="https://example.com/cb",
    )
    connector = DefaultUpstoxBrokerConnector(config=config, http=http_mock)
    with pytest.raises(BrokerAuthError):
        connector.exchange_auth_code(code=secret_code)

    all_log_output = "\n".join(captured_before)
    assert secret_code not in all_log_output, (
        "auth code leaked into log output:\n" + all_log_output
    )
    assert secret not in all_log_output, (
        "client secret leaked into log output:\n" + all_log_output
    )


def test_exchange_auth_code_does_not_leak_auth_code_or_secret_in_exception_messages():
    """The AC's redaction rule, exercised on the exception path:
    the ``BrokerAuthError`` raised on a 4xx must mention the
    reconnect instruction but NOT contain the auth code or client
    secret the caller passed in. A future regression that
    stringifies the form into the error message would surface
    here."""
    secret_code = "SECRET-AUTH-CODE-DO-NOT-LOG"
    secret = "SECRET-CLIENT-SECRET-DO-NOT-LOG"

    api_exc = BrokerApiError("Upstox returned HTTP 400")
    api_exc.http_status = 400

    http_mock = Mock()
    http_mock.post_token_exchange.side_effect = api_exc

    config = UpstoxConfig(
        client_id="harmless-client-id",
        client_secret=secret,
        redirect_uri="https://example.com/cb",
    )
    connector = DefaultUpstoxBrokerConnector(config=config, http=http_mock)

    with pytest.raises(BrokerAuthError) as excinfo:
        connector.exchange_auth_code(code=secret_code)

    msg = str(excinfo.value)
    assert secret_code not in msg
    assert secret not in msg


def test_exchange_auth_code_redacts_access_token_in_raw():
    """The ``raw`` field on the returned ``BrokerCredentials``
    must contain the full response shape (so a caller can inspect
    it), but the ``access_token`` value itself must be replaced by
    the redaction sentinel — so if ``raw`` is ever stringified
    into a log line or an exception message, the secret token
    cannot leak. The original token still lives on
    ``creds.access_token`` (the only thing the caller actually
    needs); only the ``raw`` cache is scrubbed."""
    http_mock = Mock()
    http_mock.post_token_exchange.return_value = {
        "access_token": "real-token",
        "user_id": "UPSTOX-USER-42",
        "expires_in": 3600,
    }

    connector = _connector(http_mock)
    creds = connector.exchange_auth_code(code="some-code")

    # Original token is on the dataclass.
    assert creds.access_token == "real-token"
    # Redacted copy is on the raw cache.
    assert creds.raw["access_token"] == "***"
    # The other fields round-trip to the raw cache verbatim — the
    # redaction is keyed on ``access_token`` alone, not a blanket
    # scrub of every value.
    assert creds.raw["user_id"] == "UPSTOX-USER-42"
    assert creds.raw["expires_in"] == 3600


# ---------------------------------------------------------------------
# AC: All tests use hand-authored fixtures and a mocked transport;
# no real network calls.
# ---------------------------------------------------------------------


def test_exchange_auth_code_never_contacts_the_network():
    """The connector's only network-bearing seam is
    ``_UpstoxHttp.post_token_exchange``. With a ``Mock`` standing in
    for the helper, that seam is satisfied without ``requests``
    being touched — verified by the fact that the test passes
    with a Mock whose ``post_token_exchange`` is the ONLY method
    the connector can possibly call. If the connector reached for
    any other helper method (``get``, ``post``, etc.), the Mock
    would raise ``AttributeError`` and the test would fail."""
    http_mock = Mock(spec=["post_token_exchange"])
    http_mock.post_token_exchange.return_value = {
        "access_token": "real-token",
    }

    connector = _connector(http_mock)
    creds = connector.exchange_auth_code(code="some-code")
    assert creds.access_token == "real-token"
    # And the helper was called exactly once — no accidental
    # double-call from a retry, no extra call from a logging path.
    http_mock.post_token_exchange.assert_called_once()


# ---------------------------------------------------------------------
# Defensive: a blank auth code is rejected client-side before any
# HTTP call is made.
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_code",
    ["", " ", "   ", "\t", "\n"],
    ids=["empty", "single_space", "multiple_spaces", "tab", "newline"],
)
def test_exchange_auth_code_rejects_blank_or_whitespace_code(bad_code: str):
    """A blank or whitespace-only ``code`` is rejected by the
    connector with ``ValueError`` rather than burned on a
    server-side rejection. Matches the same defensive posture
    ``build_authorize_url`` already applies for ``state``."""
    http_mock = Mock(spec=["post_token_exchange"])

    connector = _connector(http_mock)
    with pytest.raises(ValueError):
        connector.exchange_auth_code(code=bad_code)
    # No HTTP call was made — the validation runs before the
    # helper is touched.
    http_mock.post_token_exchange.assert_not_called()


# ---------------------------------------------------------------------
# QA-authored STORY-6 end-to-end check — every AC verified against
# a single fresh connector.
# ---------------------------------------------------------------------


def test_qa_story6_exchange_auth_code_exhaustive_acceptance_criteria():
    """QA-authored end-to-end check of every STORY-6 acceptance
    criterion against a single ``exchange_auth_code`` invocation
    with a fully-populated response fixture.

    Asserted ACs (all in one test, by design — a single call
    produces all the evidence):

      1. ``post_token_exchange`` is called exactly once with a
         form whose keys are exactly ``code``, ``client_id``,
         ``client_secret``, ``redirect_uri``, ``grant_type`` and
         whose values match the injected config (plus
         ``grant_type='authorization_code'``).
      2. The returned ``BrokerCredentials`` has
         ``token_type='Bearer'``.
      3. ``broker_user_id`` is populated from a top-level
         ``user_id`` field.
      4. ``expires_at`` is populated from ``expires_in`` (now +
         N seconds).
      5. ``refresh_token`` is populated from a top-level
         ``refresh_token`` field when present.
      6. ``raw`` carries the full response shape with the
         ``access_token`` value redacted.
    """
    config = UpstoxConfig(
        client_id="qa-story6-client-id",
        client_secret="qa-story6-client-secret",
        redirect_uri="https://example.com/cb?next=/foo",
    )
    http_mock = Mock()
    http_mock.post_token_exchange.return_value = {
        "access_token": "qa-story6-access-token",
        "user_id": "qa-story6-user",
        "expires_in": 3600,
        "refresh_token": "qa-story6-refresh-token",
    }

    connector = DefaultUpstoxBrokerConnector(config=config, http=http_mock)
    before = datetime.now(timezone.utc)
    creds = connector.exchange_auth_code(code="qa-story6-code")
    after = datetime.now(timezone.utc)

    # AC 1: helper called exactly once. The full form-key order,
    # the literal ``grant_type='authorization_code'``, and the
    # exact config values being passed through are pinned by the
    # dedicated ``test_exchange_auth_code_calls_post_token_exchange_with_exact_form_payload``
    # test above — re-asserting them here would duplicate the
    # same block in two tests, which is what this QA exhaustive
    # check is explicitly trying to avoid.
    http_mock.post_token_exchange.assert_called_once()

    # AC 2: token_type is the literal 'Bearer'.
    assert creds.token_type == "Bearer"

    # AC 3: broker_user_id populated from user_id.
    assert creds.broker_user_id == "qa-story6-user"

    # AC 4: expires_at populated from now + expires_in seconds.
    assert isinstance(creds.expires_at, datetime)
    assert before + timedelta(seconds=3600) <= creds.expires_at
    assert creds.expires_at <= after + timedelta(seconds=3600)

    # AC 5: refresh_token populated when present.
    assert creds.refresh_token == "qa-story6-refresh-token"

    # AC 6: raw has the full response shape with access_token redacted.
    assert creds.raw["access_token"] == "***"
    assert creds.raw["user_id"] == "qa-story6-user"
    assert creds.raw["expires_in"] == 3600
    assert creds.raw["refresh_token"] == "qa-story6-refresh-token"

    # Defensive cross-check: the secret token is on the dataclass
    # (the caller needs it), and NOT anywhere that would be
    # stringified into a log line.
    assert creds.access_token == "qa-story6-access-token"
    assert "qa-story6-code" not in repr(creds.raw)
    assert "qa-story6-client-secret" not in repr(creds.raw)


def test_qa_story6_exchange_auth_code_4xx_reconnect_message_exact_text():
    """QA-authored focused check: the 4xx ``BrokerAuthError``
    message instructs the user to restart the connect flow, with
    wording distinct enough that a generic "authentication failed"
    style message would not pass. Distinct from the parametrized
    test above so the wording is also independently verified."""
    api_exc = BrokerApiError("Upstox returned HTTP 400")
    api_exc.http_status = 400

    http_mock = Mock()
    http_mock.post_token_exchange.side_effect = api_exc

    connector = DefaultUpstoxBrokerConnector(
        config=_CONFIG, http=http_mock,
    )
    with pytest.raises(BrokerAuthError) as excinfo:
        connector.exchange_auth_code(code="some-code")
    # The message must mention "restart" or "reconnect" so the user
    # knows what to do next. "connect" alone is too ambiguous
    # (every error message in this module mentions "connect"), so
    # we look for the more specific verbs.
    msg = str(excinfo.value).lower()
    assert "restart" in msg or "reconnect" in msg


# ---------------------------------------------------------------------
# QA-author verification — a single, fresh test that exercises THIS
# story's acceptance criteria end-to-end against a hand-authored
# fixture and a mocked transport (no real network calls).
# ---------------------------------------------------------------------


def test_qa_story6_exchange_auth_code_real_story6_accepts_end_to_end():
    """QA-author end-to-end verification of STORY-6 against a fresh
    connector.

    Each acceptance criterion is asserted at least once by name,
    using a hand-authored fixture (no real Upstox call) and a
    ``Mock`` for the connector's only network-bearing seam
    (``_UpstoxHttp.post_token_exchange``):

      AC-1: The helper is called exactly once with the form-encoded
            body carrying exactly the five documented keys and
            ``grant_type='authorization_code'``.
      AC-2: A minimal fixture (only ``access_token``) yields
            ``BrokerCredentials`` with that token, ``token_type=
            'Bearer'``, ``expires_at=None``, ``refresh_token=None``,
            ``broker_user_id=None`` — no exception.
      AC-3: A fixture additionally containing ``user_id`` and
            ``expires_in`` populates ``broker_user_id`` and an
            ``expires_at`` computed as now + expires_in seconds.
      AC-4a: A 2xx response with no ``access_token`` raises
             ``BrokerApiError``.
      AC-4b: A 400/401 response raises ``BrokerAuthError`` whose
             message instructs the user to reconnect.
      AC-5: The token exchange is never retried, even on 5xx
            (asserted via ``call_count == 1`` on a 5xx-raising
            mock).
      AC-6: The auth code and client secret appear in no log output
            or exception message.
      AC-7: All tests use hand-authored fixtures and a mocked
            transport — ``Mock(spec=[...])`` here guarantees the
            connector cannot reach any other seam.
    """
    import logging

    # ---------------- AC-2 / AC-3 (happy paths) -------------------
    # AC-2: minimal fixture — only access_token, all other fields
    # must default to None, no exception.
    http_minimal = Mock()
    http_minimal.post_token_exchange.return_value = {
        "access_token": "minimal-ac2-token",
    }
    connector_minimal = _connector(http_minimal)
    creds_minimal = connector_minimal.exchange_auth_code(code="ac2-code")
    assert isinstance(creds_minimal, BrokerCredentials), (
        "AC-2: minimal fixture must yield BrokerCredentials, "
        "not raise"
    )
    assert creds_minimal.access_token == "minimal-ac2-token"
    assert creds_minimal.token_type == "Bearer", (
        "AC-2: token_type must default to 'Bearer'"
    )
    assert creds_minimal.expires_at is None, (
        "AC-2: expires_at must default to None when 'expires_in' "
        "is absent"
    )
    assert creds_minimal.refresh_token is None, (
        "AC-2: refresh_token must default to None when absent"
    )
    assert creds_minimal.broker_user_id is None, (
        "AC-2: broker_user_id must default to None when 'user_id' "
        "is absent"
    )

    # AC-3: full fixture — user_id and expires_in must populate
    # broker_user_id and a now+expires_in expires_at. The bounding
    # window (before / after) handles wall-clock drift.
    http_full = Mock()
    http_full.post_token_exchange.return_value = {
        "access_token": "full-ac3-token",
        "user_id": "UPSTOX-USER-AC3",
        "expires_in": 3600,
    }
    connector_full = _connector(http_full)
    before = datetime.now(timezone.utc)
    creds_full = connector_full.exchange_auth_code(code="ac3-code")
    after = datetime.now(timezone.utc)
    assert creds_full.broker_user_id == "UPSTOX-USER-AC3", (
        "AC-3: broker_user_id must come from a top-level user_id"
    )
    assert isinstance(creds_full.expires_at, datetime), (
        "AC-3: expires_at must be a datetime when expires_in is "
        "present"
    )
    assert before + timedelta(seconds=3600) <= creds_full.expires_at, (
        "AC-3: expires_at must be >= now + expires_in seconds "
        "(lower bound)"
    )
    assert creds_full.expires_at <= after + timedelta(seconds=3600), (
        "AC-3: expires_at must be <= now + expires_in seconds "
        "(upper bound)"
    )

    # ---------------- AC-1 (exact form payload) -------------------
    # A single fresh call, asserting the form the connector passes
    # to the helper is byte-identical to the docs' reference
    # example: keys in the order ``code``, ``client_id``,
    # ``client_secret``, ``redirect_uri``, ``grant_type``; values
    # match the injected config; ``grant_type`` is the literal
    # ``authorization_code``.
    http_ac1 = Mock()
    http_ac1.post_token_exchange.return_value = {
        "access_token": "ac1-token",
    }
    connector_ac1 = _connector(http_ac1)
    connector_ac1.exchange_auth_code(code="ac1-code")
    assert http_ac1.post_token_exchange.call_count == 1
    _, kwargs = http_ac1.post_token_exchange.call_args
    assert list(kwargs["form"].keys()) == [
        "code",
        "client_id",
        "client_secret",
        "redirect_uri",
        "grant_type",
    ], "AC-1: form must contain exactly the five documented keys in docs order"
    assert kwargs["form"]["grant_type"] == "authorization_code", (
        "AC-1: grant_type must be the literal 'authorization_code'"
    )
    assert kwargs["form"]["code"] == "ac1-code"
    assert kwargs["form"]["client_id"] == _CONFIG.client_id
    assert kwargs["form"]["client_secret"] == _CONFIG.client_secret
    assert kwargs["form"]["redirect_uri"] == _CONFIG.redirect_uri

    # ---------------- AC-4a (2xx without access_token) ------------
    http_no_tok = Mock()
    http_no_tok.post_token_exchange.return_value = {"status": "success"}
    connector_no_tok = _connector(http_no_tok)
    with pytest.raises(BrokerApiError) as exc_4a:
        connector_no_tok.exchange_auth_code(code="ac4a-code")
    assert "access_token" in str(exc_4a.value), (
        "AC-4a: BrokerApiError message must identify the missing "
        "field"
    )

    # ---------------- AC-4b (400 / 401 → BrokerAuthError) ---------
    for status_code in (400, 401):
        api_exc = BrokerApiError(f"Upstox returned HTTP {status_code}")
        api_exc.http_status = status_code
        http_4xx = Mock()
        http_4xx.post_token_exchange.side_effect = api_exc
        connector_4xx = _connector(http_4xx)
        with pytest.raises(BrokerAuthError) as exc_4b:
            connector_4xx.exchange_auth_code(code="ac4b-code")
        msg_4b = str(exc_4b.value).lower()
        assert "restart" in msg_4b or "reconnect" in msg_4b, (
            f"AC-4b: BrokerAuthError message must instruct the user "
            f"to restart the connect flow for status {status_code}; "
            f"got: {exc_4b.value!r}"
        )

    # ---------------- AC-5 (no retry, even on 5xx) ---------------
    api_5xx = BrokerApiError("Upstox returned HTTP 500")
    api_5xx.http_status = 500
    http_5xx = Mock()
    http_5xx.post_token_exchange.side_effect = api_5xx
    connector_5xx = _connector(http_5xx)
    with pytest.raises(BrokerApiError):
        connector_5xx.exchange_auth_code(code="ac5-code")
    assert http_5xx.post_token_exchange.call_count == 1, (
        "AC-5: the token exchange must be called exactly once "
        "even on 5xx — no retry"
    )

    # ---------------- AC-6 (no leakage of code or secret) ---------
    secret_code = "SECRET-AUTH-CODE-AC-6-DO-NOT-LOG"
    secret = "SECRET-CLIENT-SECRET-AC-6-DO-NOT-LOG"
    api_exc_log = BrokerApiError("Upstox returned HTTP 400")
    api_exc_log.http_status = 400

    captured: list[str] = []

    class _LogCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(self.format(record))

    handler = _LogCapture()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log_logger = logging.getLogger("components.c01_user_portfolio")
    prior_level = log_logger.level
    log_logger.setLevel(logging.DEBUG)
    log_logger.addHandler(handler)
    try:
        http_secret = Mock()
        http_secret.post_token_exchange.side_effect = api_exc_log
        cfg = UpstoxConfig(
            client_id="harmless-ac6-client-id",
            client_secret=secret,
            redirect_uri="https://example.com/cb",
        )
        connector_secret = DefaultUpstoxBrokerConnector(
            config=cfg, http=http_secret,
        )
        with pytest.raises(BrokerAuthError) as exc_6:
            connector_secret.exchange_auth_code(code=secret_code)
        all_log_output = "\n".join(captured)
        assert secret_code not in all_log_output, (
            "AC-6: auth code leaked into log output:\n"
            + all_log_output
        )
        assert secret not in all_log_output, (
            "AC-6: client secret leaked into log output:\n"
            + all_log_output
        )
        msg_6 = str(exc_6.value)
        assert secret_code not in msg_6, (
            "AC-6: auth code leaked into BrokerAuthError message"
        )
        assert secret not in msg_6, (
            "AC-6: client secret leaked into BrokerAuthError message"
        )
    finally:
        log_logger.removeHandler(handler)
        log_logger.setLevel(prior_level)

    # ---------------- AC-7 (hand-authored fixtures, mocked
    # transport — verified by structure) ------------------------
    # The ``Mock(spec=["post_token_exchange"])`` guarantees the
    # connector cannot reach any other seam: if it tried to call
    # ``get``, ``post``, or any other attribute, ``Mock`` would
    # raise ``AttributeError`` immediately, before the assertion.
    http_strict = Mock(spec=["post_token_exchange"])
    http_strict.post_token_exchange.return_value = {
        "access_token": "ac7-token",
    }
    connector_strict = DefaultUpstoxBrokerConnector(
        config=_CONFIG, http=http_strict,
    )
    creds_strict = connector_strict.exchange_auth_code(code="ac7-code")
    assert creds_strict.access_token == "ac7-token"
    http_strict.post_token_exchange.assert_called_once()