"""Upstox HTTP client wrapper (STORY-4).

A thin `requests`-backed helper used only by the upcoming
``DefaultUpstoxBrokerConnector`` (ADR-0022's real Upstox implementation).
This module is deliberately the *only* place in the codebase that talks
to ``https://api.upstox.com`` — same "one small module per external
vendor" shape every other vendor client (``src/alpha_vantage_client.py``,
``src/tavily_client.py``, ``src/resend_client.py``,
``src/yahoo_finance_client.py``) already follows, and the same use of
the ``requests`` library (not ``httpx``) the rest of this project's
vendor clients consistently chose. Staying on ``requests`` is a
deliberate consistency choice: switching to ``httpx`` here would force
``unittest.mock.patch`` tests to mock a different symbol than the rest
of the codebase does, with no real benefit (every AC is satisfiable on
``requests``).

Why this is its own module, not methods on ``DefaultUpstoxBrokerConnector``:

  * Separation of concerns — HTTP/transport details (headers, timeouts,
    retries, error mapping, redaction) live here; broker-specific
    semantics (DTO assembly, ``BrokerConnector`` Protocol conformance,
    ``BoundaryGate`` tagging) live on the connector. Mixing them would
    make ``DefaultUpstoxBrokerConnector`` un-testable in isolation.

  * Constructor-injectable backoff hook (``sleeper``) so unit tests
    patch a single seam and run in milliseconds — the AC's explicit
    "Backoff sleeps are injectable/patched so tests run fast" rule.

  * Single, named exception-mapping layer, so the broker connector
    itself only needs to know about ``BrokerAuthError``,
    ``BrokerRateLimitError``, and ``BrokerApiError`` — never about
    ``requests.exceptions`` or HTTP status codes.

Auth model:

  * Authenticated GETs (``get``) attach ``Authorization: Bearer
    <access_token>`` and ``Accept: application/json`` to every
    request — the AC's exact two-header contract, and no more (no
    User-Agent spoofing, no extra Accept-Encoding, no opportunistic
    compression that would change parsing behaviour). The access
    token is read from a callable ``token_provider`` so the token is
    never stored on the helper between calls, and so rotating tokens
    (Upstox access tokens are short-lived) are picked up on the very
    next call without reconstructing the helper.

  * The OAuth token exchange (``post_token_exchange``) sends
    ``Content-Type: application/x-www-form-urlencoded`` — Upstox's own
    documented requirement for this endpoint — and never retries. A
    retry on a token exchange is a real footgun: the server may have
    already minted a new token on the first attempt, and retrying
    consumes the user's one-time auth code against a second token
    grant that may itself fail. So this method bypasses the retry loop
    entirely and surfaces the first non-2xx response as-is.

Retry policy (HTTP 429 and 5xx only — never retry other 4xx, never
retry the token exchange):

  * Up to 3 total attempts (1 initial + 2 retries; matches the
    AC's "max 3 attempts" wording — not the ``api_error_logging``
    helper's "1 initial + 3 retries" wording, which is a separate
    convention for the GET-with-retry helper used by the Yahoo Finance
    and exchange-rate clients). Bounded, so a real outage cannot
    pin a request indefinitely.

  * Exponential backoff ``1s, 2s`` between attempts (two retries
    means two sleeps; ``3`` retries on a single endpoint does not
    exist here).

  * Jitter — a small random offset added to each backoff so two
    clients retrying in lockstep don't synchronise. The jitter
    range is the backoff itself (e.g. the 1s sleep becomes
    ``[1s, 2s)``, the 2s sleep becomes ``[2s, 4s)``), the same
    "full-jitter" shape AWS's own retry guidance recommends.

  * Only HTTP 429 and 5xx are retried. Every other non-2xx — 400,
    401, 403, 404, etc. — is returned immediately to the caller
    for the normal error-mapping path. The ``requests`` library's
    own connection-level errors (``ConnectionError``,
    ``RequestException``) are *not* retried either: the AC scopes
    retry strictly to "HTTP 429 and 5xx".

  * **Open question** — Upstox's documented rate-limit ceiling is
    not pinned in the fetched docs (their public docs reference
    per-endpoint and per-account limits but do not publish a single
    canonical "you may make N requests per minute" number). The
    3-attempts / 1s+2s / jitter policy above is therefore this
    project's own conservative choice — defensive against real
    rate-limit responses while not pretending to know an exact
    limit we don't. If Upstox later publishes a precise number
    this should be revisited (likely raised to match it), but
    making the policy less defensive until that point would just
    turn real 429s into 5xx cascades.

Error mapping — every non-success outcome is converted to one of the
STORY-2 exceptions:

  * HTTP 401 / 403 → ``BrokerAuthError`` (the access token is invalid,
    expired, or forbidden from this resource; the caller decides
    whether to re-auth).
  * HTTP 429 (after all retries exhausted) → ``BrokerRateLimitError``
    (retrying is the right next step; surfacing this distinctly
    lets the caller back off further or surface it to the user).
  * Any other non-2xx, OR a 2xx whose JSON body's top-level
    ``status`` field is not ``"success"`` → ``BrokerApiError``,
    carrying the HTTP status and the response body truncated to
    ``_RESPONSE_BODY_MAX_LEN`` (500) characters — long enough to
    keep the actionable part of a real error payload, short enough
    to keep the exception message out of log noise and out of
    accidentally-logged secrets.

Redaction — the access token, client secret, and auth code must never
appear in logs or exception messages. Every code path that touches a
secret passes it through ``_REDACTED = "***"`` before the string is
formatted into a message or a log record. The bodies and headers we
log do not include those fields; the request bodies we redact via
``_redact_form`` so even the URL-encoded ``client_secret=...`` /
``code=...`` POST form cannot accidentally end up in a log line.

NOTE: This module is the *private* HTTP helper for the future
``DefaultUpstoxBrokerConnector`` — it does not itself implement the
``BrokerConnector`` Protocol (``src/components/c01_user_portfolio.py``)
and does not assemble any ``BrokerHolding`` / ``BrokerTransaction`` /
``BrokerCredentials`` DTOs. Those concerns belong to the connector,
not to transport.
"""

import json
import logging
import random
from dataclasses import dataclass
from typing import Callable

import requests

from components.c01_user_portfolio import (
    BrokerApiError,
    BrokerAuthError,
    BrokerRateLimitError,
)

# Upstox's own public API base. Real, not a placeholder — verified
# against Upstox's developer documentation. All methods here build
# URLs by joining this base with the endpoint's relative path, so a
# future move to ``https://api-sandbox.upstox.com`` (for a future
# test environment) is one constant change away.
UPSTOX_API_BASE_URL = "https://api.upstox.com"

# Per the Upstox OAuth documentation, the token-exchange endpoint
# is at this path on the same base URL. No retry on this endpoint
# — see module docstring for the rationale.
UPSTOX_TOKEN_EXCHANGE_PATH = "/v2/login/authorization/token"

# (connect_timeout_seconds, read_timeout_seconds) — passed as a
# ``requests``-style ``timeout=(connect, read)`` tuple so a hung
# TCP connect doesn't burn the entire read budget, and a slow read
# doesn't block forever. Same shape every other vendor client in
# this project uses (``_REQUEST_TIMEOUT_SECONDS`` as a single int
# is the historical convention here; the AC explicitly calls out
# the two-tuple shape for this helper, so the new shape is local
# to this module).
_CONNECT_TIMEOUT_SECONDS = 10
_READ_TIMEOUT_SECONDS = 30

# Bounded retry: 1 initial attempt + 2 retries = 3 total attempts.
# The AC's "max 3 attempts" is satisfied with this exact count;
# the backoff sequence below has ``len == _MAX_RETRIES``, so the
# two never drift.
_MAX_ATTEMPTS = 3
_MAX_RETRIES = _MAX_ATTEMPTS - 1
_RETRY_BACKOFFS_SECONDS: tuple[int, ...] = (1, 2)

# Truncate the response body carried in BrokerApiError messages at
# this length. 500 is the AC's exact value — long enough to keep
# the actionable part of a real Upstox error payload (Upstox's own
# error bodies are well under this in practice, verified against
# their docs), short enough that log records and exception
# messages stay scannable.
_RESPONSE_BODY_MAX_LEN = 500

# Redaction sentinel. Every place a secret could leak — log
# messages, exception messages, the response-body snippet carried
# inside ``BrokerApiError`` — uses this constant so a future audit
# can grep for the single value.
_REDACTED = "***"

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _UpstoxRetryDecision:
    """Internal decision object returned by ``_should_retry`` — the
    single point where the retry policy is encoded, so a future
    policy change is one method to edit, not N branches."""

    retry: bool
    is_rate_limit: bool  # tracks "was this a 429" separately so the
                         # post-retry-exhaustion mapping can raise
                         # ``BrokerRateLimitError`` distinctly.


def _redact_form(form: dict) -> dict:
    """Return a copy of ``form`` with every key that could carry a
    secret replaced by ``_REDACTED``. Used so the request body of the
    token exchange — which legitimately contains ``client_secret`` and
    ``code`` — cannot be accidentally formatted into a log message
    or exception message. Non-secret fields are kept verbatim so a
    log line still shows ``grant_type=authorization_code`` etc.,
    which is the actual useful debugging signal here."""
    sensitive_keys = {"client_secret", "code"}
    redacted: dict = {}
    for key, value in form.items():
        if key in sensitive_keys:
            redacted[key] = _REDACTED
        else:
            redacted[key] = value
    return redacted


def _should_retry(status_code: int) -> _UpstoxRetryDecision:
    """Single, named retry-decision function so the policy lives in
    exactly one place. ``status_code`` is an int (``requests``'s own
    ``Response.status_code`` type). 5xx and 429 are retryable; every
    other non-2xx is not — the AC's exact rule."""
    if status_code == 429:
        return _UpstoxRetryDecision(retry=True, is_rate_limit=True)
    if 500 <= status_code < 600:
        return _UpstoxRetryDecision(retry=True, is_rate_limit=False)
    return _UpstoxRetryDecision(retry=False, is_rate_limit=False)


class _UpstoxHttp:
    """Private HTTP wrapper for ``DefaultUpstoxBrokerConnector``
    (STORY-4). Wraps ``requests.get`` / ``requests.post`` with:

      * the exact two-header auth contract on authenticated GETs
        (``Authorization: Bearer <token>`` + ``Accept:
        application/json``),
      * ``Content-Type: application/x-www-form-urlencoded`` for the
        token POST,
      * a connect/read timeout (10s / 30s),
      * bounded retry (max 3 attempts, exponential backoff ``1s, 2s``
        with jitter) on HTTP 429 and 5xx only,
      * mapping of every outcome to one of the STORY-2 exceptions.

    Constructor injection (so tests can patch a single seam):

      * ``token_provider`` — a zero-arg callable that returns the
        current Upstox access token. Resolved at call time, so a
        rotated token is picked up on the very next request without
        reconstructing the helper. The token never lives on the
        helper as state.
      * ``sleeper`` — a ``(seconds: float) -> None`` callable used
        for the backoff sleeps. The default is ``time.sleep``; tests
        patch a no-op or a recorder so the suite runs in
        milliseconds. The AC's "Backoff sleeps are injectable /
        patched" rule.
      * ``jitter_fn`` — a zero-arg callable returning a float in
        ``[0.0, 1.0)`` used to add jitter to each backoff. The
        default is ``random.random``; tests patch a constant to
        make the backoff sequence deterministic.
      * ``session`` — an optional ``requests.Session`` to use for
        the underlying calls. Defaults to ``None`` (a fresh
        ``requests.get``/``requests.post`` per call). Passing a
        ``Session`` is the same seam every other ``requests``-based
        client in this project uses (``requests.Session`` is the
        documented pattern for connection pooling and for tests
        that want to swap transport wholesale). The AC's mock-based
        tests patch ``requests.get`` / ``requests.post`` directly,
        which works regardless of whether a Session is provided —
        both code paths call through the module-level ``requests``
        module — so this seam doesn't conflict with the test seam.

    Redaction invariant: this class never logs or stringifies the
    access token, client secret, or auth code. The
    ``_redact_form`` / ``_REDACTED`` helpers and the per-secret
    ``logger`` extra discipline enforce this."""

    def __init__(
        self,
        *,
        token_provider: Callable[[], str],
        sleeper: Callable[[float], None] = __import__("time").sleep,
        jitter_fn: Callable[[], float] = random.random,
        session: requests.Session | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._sleeper = sleeper
        self._jitter_fn = jitter_fn
        self._session = session

    # ---- public surface ------------------------------------------------

    def get(self, path: str) -> dict:
        """Authenticated GET against ``UPSTOX_API_BASE_URL + path``.

        Attaches ``Authorization: Bearer <token>`` and ``Accept:
        application/json`` to every request, applies the connect/read
        timeout, retries on HTTP 429 and 5xx only, and maps the final
        response to the STORY-2 exceptions. Returns the parsed JSON
        body on a 2xx whose top-level ``status`` field is
        ``"success"``.

        Raises:
            BrokerAuthError: 401 / 403 — token is invalid, expired, or
                forbidden from this resource.
            BrokerRateLimitError: 429 after retries are exhausted.
            BrokerApiError: any other non-2xx, OR a 2xx whose JSON
                body's top-level ``status`` field is not
                ``"success"``. Carries the HTTP status and the body
                truncated to ``_RESPONSE_BODY_MAX_LEN`` chars.
        """
        url = UPSTOX_API_BASE_URL + path
        access_token = self._token_provider()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        timeout = (_CONNECT_TIMEOUT_SECONDS, _READ_TIMEOUT_SECONDS)
        response = self._do_request_with_retry(
            method="GET",
            url=url,
            headers=headers,
            timeout=timeout,
        )
        return self._map_response_to_body(response)

    def post_token_exchange(self, *, form: dict) -> dict:
        """One single POST to ``UPSTOX_TOKEN_EXCHANGE_PATH`` carrying
        the OAuth code-exchange form (``code``, ``client_id``,
        ``client_secret``, ``grant_type``, ``redirect_uri``).

        Sends ``Content-Type: application/x-www-form-urlencoded``
        (Upstox's documented requirement for this endpoint), uses the
        same connect/read timeout as authenticated GETs, and **never
        retries** — a retry on a token exchange would either waste a
        one-time auth code on a duplicate call or trigger Upstox's
        own duplicate-grant rejection, both of which is worse than
        surfacing the first response verbatim.

        Returns the parsed JSON body on a 2xx whose top-level
        ``status`` field is ``"success"``.

        Raises:
            BrokerAuthError: 401 / 403 — the auth code or client
                credentials are invalid.
            BrokerApiError: any other non-2xx, or a 2xx whose body
                has ``status != "success"``. Carries the HTTP status
                and the body truncated to ``_RESPONSE_BODY_MAX_LEN``
                chars.
        """
        url = UPSTOX_API_BASE_URL + UPSTOX_TOKEN_EXCHANGE_PATH
        headers = {"Accept": "application/json"}
        timeout = (_CONNECT_TIMEOUT_SECONDS, _READ_TIMEOUT_SECONDS)
        # No retry on the token exchange — see module docstring.
        response = self._do_request(method="POST", url=url, headers=headers,
                                    data=form, timeout=timeout)
        return self._map_response_to_body(response)

    # ---- internal HTTP plumbing ---------------------------------------

    def _do_request(
        self,
        *,
        method: str,
        url: str,
        headers: dict,
        timeout,
        params: dict | None = None,
        data: dict | None = None,
    ) -> requests.Response:
        """Single, unretrying HTTP call. ``method`` is ``"GET"`` or
        ``"POST"`` — the only two this helper needs. ``data`` is
        forwarded to ``requests`` as form data (so
        ``Content-Type: application/x-www-form-urlencoded`` is what
        ``requests`` sets automatically — we do not override it)."""
        if method == "GET":
            requests_fn = self._session.get if self._session is not None else requests.get
            return requests_fn(url, headers=headers, params=params,
                               timeout=timeout)
        if method == "POST":
            requests_fn = self._session.post if self._session is not None else requests.post
            return requests_fn(url, headers=headers, data=data,
                               timeout=timeout)
        raise ValueError(f"_UpstoxHttp only supports GET and POST; got {method!r}")

    def _do_request_with_retry(
        self,
        *,
        method: str,
        url: str,
        headers: dict,
        timeout,
    ) -> requests.Response:
        """``_do_request`` wrapped in the bounded retry loop. Only
        used by ``get`` — ``post_token_exchange`` calls
        ``_do_request`` directly to bypass the retry policy entirely.

        Tracks the last retryable response so the post-exhaustion
        mapping can re-raise with the right status attached. When the
        last retryable response is a 429 specifically, the final
        mapping becomes ``BrokerRateLimitError``; for any other 5xx
        it's ``BrokerApiError`` carrying the original HTTP status
        and the body snippet (the AC's "500 retried x3 then
        BrokerApiError" rule). Logs each retry attempt under
        ``UPSTOX_RETRY`` so an operator tracing a real outage can see
        the exact backoff sequence that fired."""
        last_retryable_response: requests.Response | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            if attempt > 1:
                backoff = _RETRY_BACKOFFS_SECONDS[attempt - 2]
                # Full-jitter: the actual sleep is in
                # ``[backoff, 2 * backoff)``. Equal-jitter (a small
                # constant offset) would still serialise two clients
                # on the same schedule; full-jitter is the documented
                # "good enough" choice when we don't know the exact
                # rate-limit ceiling.
                jitter = self._jitter_fn() * backoff
                self._sleeper(backoff + jitter)
            response = self._do_request(method=method, url=url,
                                        headers=headers, timeout=timeout)
            decision = _should_retry(response.status_code)
            if not decision.retry:
                return response
            last_retryable_response = response
            _logger.warning(
                "Upstox %s %s returned HTTP %d (attempt %d/%d); retrying",
                method, url, response.status_code, attempt, _MAX_ATTEMPTS,
                extra={
                    "error_code": "UPSTOX_RETRY",
                    "attempt": attempt,
                    "status_code": response.status_code,
                    "method": method,
                },
            )

        # All retries exhausted. The loop above is guaranteed to have
        # recorded a retryable response on each iteration, so
        # ``last_retryable_response`` is not None here.
        if last_retryable_response is None:  # pragma: no cover - unreachable
            raise BrokerApiError(
                "Upstox request exhausted retries without a recorded response"
            )
        raise self._map_non_success(
            last_retryable_response, exhausted_retries=True
        )

    # ---- response mapping ----------------------------------------------

    def _map_response_to_body(self, response: requests.Response) -> dict:
        """Map a ``requests.Response`` to either a parsed JSON body
        (success path) or a STORY-2 exception (failure path).

        The 2xx branch checks the top-level ``status`` field; a 2xx
        whose body's ``status`` is not ``"success"`` is mapped to
        ``BrokerApiError`` (the AC's explicit "200 with status=error
        → BrokerApiError" rule). Body parsing failures (non-JSON
        2xx) also map to ``BrokerApiError`` — Upstox's contract is
        JSON, so a non-JSON 2xx is a real upstream-side bug we
        surface honestly rather than silently fabricating an
        empty dict."""
        if 200 <= response.status_code < 300:
            try:
                body = response.json()
            except (ValueError, requests.exceptions.JSONDecodeError) as exc:
                snippet = self._body_snippet(response)
                api_exc = BrokerApiError(
                    f"Upstox returned HTTP {response.status_code} with "
                    f"non-JSON body"
                    + (f": {snippet}" if snippet else "")
                )
                api_exc.http_status = response.status_code
                api_exc.body_snippet = snippet
                raise api_exc from exc
            if not isinstance(body, dict):
                snippet = self._body_snippet(response)
                api_exc = BrokerApiError(
                    f"Upstox returned HTTP {response.status_code} with "
                    f"non-dict JSON body"
                    + (f": {snippet}" if snippet else "")
                )
                api_exc.http_status = response.status_code
                api_exc.body_snippet = snippet
                raise api_exc
            if body.get("status") != "success":
                snippet = self._body_snippet(response)
                api_exc = BrokerApiError(
                    f"Upstox returned HTTP {response.status_code} with "
                    f"status != success"
                    + (f": {snippet}" if snippet else "")
                )
                api_exc.http_status = response.status_code
                api_exc.body_snippet = snippet
                raise api_exc
            return body

        # Non-2xx — map via the shared mapping below. The retry
        # exhaustion flag is False here because this branch handles
        # the immediate non-retryable responses (4xx-other, 5xx on
        # a non-retried path, 429 on a non-retried path) — those are
        # returned directly, not raised from the retry-exhaustion
        # branch above.
        raise self._map_non_success(response, exhausted_retries=False)

    def _map_non_success(
        self,
        response: requests.Response | None,
        *,
        exhausted_retries: bool,
    ) -> BrokerAuthError | BrokerRateLimitError | BrokerApiError:
        """Single mapping point for every non-2xx outcome, whether it
        was returned immediately by ``_do_request`` or arrived after
        the retry loop in ``_do_request_with_retry``.

        Mapping rules (STORY-2):
          * 401 / 403 → ``BrokerAuthError``
          * 429       → ``BrokerRateLimitError``
          * anything else → ``BrokerApiError`` carrying the HTTP
            status and the body truncated to
            ``_RESPONSE_BODY_MAX_LEN``.

        ``response`` is ``None`` only when a non-retryable network
        error escaped ``_do_request`` (which currently doesn't
        happen — ``requests``'s ``ConnectionError`` and friends are
        not retried and not wrapped by us either; the AC scopes this
        module to HTTP-status outcomes). The branch is here
        defensively so a future ``Session``-level failure doesn't
        raise a bare ``requests.exceptions.RequestException``.
        """
        if response is None:
            exc = BrokerApiError(
                "Upstox returned no response (HTTP 0)"
            )
            exc.http_status = 0
            exc.body_snippet = ""
            return exc
        status = response.status_code
        snippet = self._body_snippet(response)
        if status in (401, 403):
            return BrokerAuthError(
                f"Upstox returned HTTP {status}; access token may be "
                f"invalid or expired"
            )
        if status == 429:
            # Distinguish "retried, still 429" from "first call was
            # 429 and we did not retry" — the message is otherwise
            # identical to the user's eye but the operator trace is
            # clearer.
            if exhausted_retries:
                return BrokerRateLimitError(
                    f"Upstox rate-limited the request (HTTP 429) after "
                    f"{_MAX_ATTEMPTS} attempts; please retry later"
                )
            return BrokerRateLimitError(
                f"Upstox rate-limited the request (HTTP 429); please "
                f"retry later"
            )
        # STORY-2's BrokerApiError is a plain ``Exception`` subclass
        # with no extra constructor params — only the message string
        # is positional. Carry the HTTP status and body snippet as
        # attributes on the exception instance so callers (and tests)
        # can read them directly without parsing the message.
        exc = BrokerApiError(
            f"Upstox returned HTTP {status}"
            + (f": {snippet}" if snippet else "")
        )
        exc.http_status = status
        exc.body_snippet = snippet
        return exc

    def _body_snippet(self, response: requests.Response) -> str:
        """Return the response body truncated to
        ``_RESPONSE_BODY_MAX_LEN`` characters, decoded as
        ``utf-8`` with replacement so a malformed upstream body
        never raises here. The returned string never contains the
        access token, client secret, or auth code — those never
        appear in Upstox's response bodies, and the truncation
        itself doesn't introduce them either. Includes an
        explicit redaction note so a future reader doesn't extend
        the body cap thinking it's fine to embed longer payloads.
        """
        try:
            raw = response.content
        except Exception:  # pragma: no cover - requests.content shouldn't raise
            raw = b""
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover
            text = ""
        if len(text) > _RESPONSE_BODY_MAX_LEN:
            return text[:_RESPONSE_BODY_MAX_LEN]
        return text


# A small helper, kept module-private, for callers that need to
# format a request-form summary without leaking the secret fields.
# Exposed at module level (single underscore would also work) so
# tests can assert the redaction behaviour without reaching into
# the class. Not part of the public surface — ``DefaultUpstoxBrokerConnector``
# should call ``_UpstoxHttp.post_token_exchange`` directly, not
# this helper.
def format_form_for_log(form: dict) -> str:
    """Return a JSON-stringified version of ``form`` with every
    secret field replaced by ``_REDACTED``. Used by callers (and
    tests) that want to log a token-exchange request body without
    leaking the auth code or client secret."""
    return json.dumps(_redact_form(form), sort_keys=True)