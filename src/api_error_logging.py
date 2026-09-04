"""Shared HTTP-retry and structured-error-logging helper used by every
external vendor client in this project. Imported by
`src/yahoo_finance_client.py` and `src/exchange_rate_client.py` (the
two real vendor clients STORY-12 scopes) and by future vendor
clients that follow the same shape.

Provides:
  - `http_get_with_retry`: one real HTTP GET with exponential-backoff
    retry (1s, 2s, 4s -- 3 retries, total possible wait = 7s) on
    rate-limit (HTTP 429) and `requests.exceptions.Timeout`. After
    retries are exhausted, logs the real underlying error via the
    structured logger and raises a clean `requests` exception
    (Timeout or HTTPError) with a user-friendly message -- the
    caller's existing network/HTTP error handling treats it
    normally.

  - `get_logger`: thin convenience wrapper that returns the stdlib
    `logging.getLogger(name)` for module-level use. The stdlib
    `logging` module is the right fit for this project's error-logging
    surface -- no existing Postgres `error_log` table is wired up
    here (verified by grepping for `error_log`/`errorlogging` across
    the codebase), and inventing a table purely for log persistence
    would couple every API failure to a Postgres write that the
    application code itself doesn't otherwise depend on. Structured
    `extra={...}` fields flow through the project's existing log
    handlers (and stdout in tests), so an aggregator downstream can
    parse the `error_code` field without a schema migration.

STORY-12: the structured `error_code` extra is the project's first
real error-code taxonomy for vendor-client failures. Codes take the
form `{PREFIX}_{REASON}` where PREFIX identifies the vendor
(`YAHOO`, `EXCHANGE_RATE_PRIMARY`, `EXCHANGE_RATE_FALLBACK`) and
REASON is one of `TIMEOUT`, `RATE_LIMIT`, `TIMEOUT_EXHAUSTED`,
`RATE_LIMIT_EXHAUSTED`, `HTTP_ERROR`, `PARSE_ERROR`. The codes are
stable strings so a downstream log aggregator or alerting rule can
match them without parsing free-form messages.

The 'p95 latency < 2 seconds' criterion is a production monitoring
target, not a unit-test assertion -- the same reasoning STORY-9 (the
caching story) applied to its 80%-cache-hit ratio: any timing
assertion in a unit test would be measuring the test harness, not
the real production behaviour, and would flake in CI. Operators
verify the p95 target against real production traces; this module
deliberately does not fabricate a timing assertion.
"""

import logging
import time

import requests

# 3 retries with exponential backoff -- total possible wait = 1+2+4 = 7s,
# matching the acceptance criterion exactly.
_RETRY_BACKOFFS_SECONDS: tuple[int, ...] = (1, 2, 4)
_MAX_ATTEMPTS = len(_RETRY_BACKOFFS_SECONDS) + 1  # 1 initial + 3 retries


def get_logger(name: str) -> logging.Logger:
    """Returns the stdlib logger for `name`. Thin wrapper so vendor
    clients import a single helper module rather than `logging` directly
    -- and so future additions (e.g. a project-wide formatter) have one
    place to live. Mirrors `logging.getLogger` semantics, including the
    fact that repeated calls with the same `name` return the same
    logger instance (the stdlib caches by name)."""
    return logging.getLogger(name)


def http_get_with_retry(
    *,
    logger: logging.Logger,
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int | float,
    error_code_prefix: str,
    extra_fields: dict | None = None,
) -> requests.Response:
    """One real HTTP GET to `url` with exponential-backoff retry on
    rate-limit (HTTP 429) and `requests.exceptions.Timeout`.

    Returns the final `requests.Response` object as soon as the call
    succeeds with any status code other than 429 -- the caller
    inspects `response.status_code` (typically via
    `response.raise_for_status()`) and decides what to do with
    non-2xx responses.

    Retries are NOT triggered on other failure modes: a non-2xx
    response that is not 429 (e.g. 404 invalid symbol, 500 server
    error) is returned to the caller immediately for the normal
    error-handling path. Network errors other than `Timeout`
    (`ConnectionError`, etc.) propagate immediately too -- the AC
    scopes retry to 429 and Timeout specifically.

    After all 4 attempts (1 initial + 3 retries) are exhausted,
    logs the real underlying error via `logger.error(...)` with a
    structured `error_code` extra (`{PREFIX}_TIMEOUT_EXHAUSTED` or
    `{PREFIX}_RATE_LIMIT_EXHAUSTED`), then raises:
      - `requests.exceptions.Timeout` with a user-friendly message
        when the cause was timeout retries exhausted;
      - `requests.exceptions.HTTPError` with a user-friendly message
        and the last 429 `response` attached, when the cause was
        rate-limit retries exhausted.
    In both cases the original underlying exception is chained via
    `__cause__` so debugging has the real trace, while the
    exception's `str()` is genuinely user-friendly (no raw stack
    trace, no vendor JSON dumped into the message).

    `time.sleep` is called for each backoff so the total possible
    wait is `sum(_RETRY_BACKOFFS_SECONDS) = 7s` -- matching the AC
    exactly. Tests monkeypatch `time.sleep` on this helper's module
    (`api_error_logging.time.sleep`) to make retry tests instant,
    not by skipping the retry loop.
    """
    extra = dict(extra_fields or {})
    last_timeout_exc: requests.exceptions.Timeout | None = None
    last_429_response: requests.Response | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        # `backoff` is always defined: 0 on the first attempt (no
        # sleep before the initial request), then the documented
        # exponential-backoff seconds for each subsequent retry.
        # Referenced in log messages below so it must be bound even
        # when no sleep actually happened yet.
        backoff = 0 if attempt == 1 else _RETRY_BACKOFFS_SECONDS[attempt - 2]
        if attempt > 1:
            time.sleep(backoff)
        try:
            # Only pass `headers` when actually provided — keeping
            # the call site identical to a plain `requests.get` when
            # no headers are configured (no kwargs are silently
            # forwarded as `None`).
            get_kwargs = {"params": params, "timeout": timeout}
            if headers is not None:
                get_kwargs["headers"] = headers
            response = requests.get(url, **get_kwargs)
        except requests.exceptions.Timeout as exc:
            last_timeout_exc = exc
            logger.warning(
                "%s request timed out (attempt %d/%d); retrying after %ds: %s",
                url, attempt, _MAX_ATTEMPTS, backoff, exc,
                extra={
                    **extra,
                    "error_code": f"{error_code_prefix}_TIMEOUT",
                    "attempt": attempt,
                },
            )
            continue
        if response.status_code == 429:
            last_429_response = response
            logger.warning(
                "%s request rate-limited (HTTP 429, attempt %d/%d); "
                "retrying after %ds",
                url, attempt, _MAX_ATTEMPTS, backoff,
                extra={
                    **extra,
                    "error_code": f"{error_code_prefix}_RATE_LIMIT",
                    "attempt": attempt,
                },
            )
            continue
        return response

    # Retries exhausted -- log the real underlying error and raise a
    # clean, user-friendly exception so the caller's existing error
    # handling path treats it normally.
    if last_timeout_exc is not None:
        logger.error(
            "%s request exhausted %d retries (last error: Timeout: %s)",
            url, _MAX_ATTEMPTS, last_timeout_exc,
            extra={
                **extra,
                "error_code": f"{error_code_prefix}_TIMEOUT_EXHAUSTED",
                "attempts": _MAX_ATTEMPTS,
            },
        )
        raise requests.exceptions.Timeout(
            f"Upstream service did not respond within {timeout}s "
            f"after {_MAX_ATTEMPTS - 1} retries. Please try again shortly."
        ) from last_timeout_exc
    # last_429_response is not None
    logger.error(
        "%s request exhausted %d retries (still rate-limited, HTTP 429)",
        url, _MAX_ATTEMPTS,
        extra={
            **extra,
            "error_code": f"{error_code_prefix}_RATE_LIMIT_EXHAUSTED",
            "attempts": _MAX_ATTEMPTS,
        },
    )
    # Chain from a synthesized `HTTPError("HTTP 429", response=...)` so
    # the user-friendly final exception has a real `__cause__` for
    # debugging (mirrors the natural `raise_for_status()` flow that
    # would have produced the same exception in non-retry code).
    upstream_http_error = requests.exceptions.HTTPError(
        f"HTTP 429 from {url}", response=last_429_response
    )
    raise requests.exceptions.HTTPError(
        f"Upstream service is rate-limiting requests (HTTP 429) "
        f"after {_MAX_ATTEMPTS - 1} retries. Please try again shortly.",
        response=last_429_response,
    ) from upstream_http_error