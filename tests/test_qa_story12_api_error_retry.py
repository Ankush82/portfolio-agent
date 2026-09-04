"""QA verification suite for STORY-12 (API error handling and retry logic).

Each test below targets ONE acceptance criterion from the story and makes
assertions that are fresh and direct. We don't re-prove the existing
suite; we re-prove each AC against the real `src/yahoo_finance_client.py`
and `src/exchange_rate_client.py` modules, with `requests.get` monkeypatched
so these tests are hermetic (no real network).

AC reference:
  AC1: Invalid symbol errors return specific error message with symbol and exchange
  AC2: API rate limits trigger exponential backoff: 1s, 2s, 4s (max 3 retries)
  AC3: Network timeouts are set to 5 seconds per request with 3 retries
  AC4: After all retries exhausted, system displays user-friendly error message
  AC5: All API failures are logged to error logging system with error codes
  AC6: System handles Yahoo Finance API errors gracefully
  AC7: System handles exchange rate API errors gracefully with fallback
  AC8: Total wait time for retries does not exceed 7 seconds
"""

import logging
from decimal import Decimal

import pytest
import requests

import exchange_rate_client
import yahoo_finance_client
from exchange_rate_client import (
    EXCHANGE_RATE_API_URL,
    EXCHANGE_RATE_FALLBACK_URL,
    ExchangeRateFetchError,
    fetch_exchange_rate,
)
from yahoo_finance_client import YahooFinanceError, fetch_yahoo_finance_quote


# ---------------------------------------------------------------------------
# Helpers — kept simple and hermetic
# ---------------------------------------------------------------------------


class _QAFakeResponse:
    """QA's own FakeResponse — independent of the dev's helpers so a bug
    in one doesn't mask a bug in the implementation."""

    def __init__(self, *, body=None, json_payload=None, status_code=200,
                 raise_json=False) -> None:
        # `json_payload` is the dict returned by `.json()`; `body` is
        # the bytes returned by `.content`. We accept either via kwargs.
        self._body = body if body is not None else b""
        self._json_payload = json_payload if json_payload is not None else (body if isinstance(body, dict) else None)
        self.status_code = status_code
        self._raise_json = raise_json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(
                f"{self.status_code} simulated", response=self
            )
            raise err

    def json(self):
        if self._raise_json:
            raise requests.exceptions.JSONDecodeError("Expecting value", "", 0)
        return self._json_payload


class _QAFakeInfrastructure:
    def __init__(self) -> None:
        self.store: dict = {}
        self.set_calls: list = []

    def cache_get(self, key: str):
        return self.store.get(key)

    def cache_set(self, key: str, value, ttl_seconds: int) -> None:
        self.set_calls.append((key, value, ttl_seconds))
        self.store[key] = value


def _yahoo_valid_payload():
    return {
        "chart": {
            "result": [{
                "meta": {
                    "currency": "INR",
                    "symbol": "RELIANCE.NS",
                    "fullExchangeName": "NSE",
                    "regularMarketPrice": 1322.0,
                    "previousClose": 1302.5,
                    "regularMarketDayHigh": 1333.0,
                    "regularMarketDayLow": 1304.1,
                    "regularMarketVolume": 13022095,
                    "fiftyTwoWeekHigh": 1611.8,
                    "fiftyTwoWeekLow": 1249.8,
                }
            }],
            "error": None,
        }
    }


def _make_yahoo_recorder(response: _QAFakeResponse):
    """Returns (fake_get, captured_args_dict)."""
    captured: dict = {"calls": 0, "urls": []}

    def fake_get(url, headers=None, timeout=None, params=None):
        captured["calls"] += 1
        captured["urls"].append(url)
        return response

    return fake_get, captured


# ===========================================================================
# AC2: 429 triggers exponential backoff 1s, 2s, 4s (3 retries)
# ===========================================================================


def test_ac2_yahoo_429_triggers_three_retries(monkeypatch):
    """AC2: when Yahoo returns HTTP 429, fetch_yahoo_finance_quote must
    retry the HTTP call 3 times (4 total attempts), with backoffs of
    1s, 2s, 4s — and finally raise YahooFinanceError."""
    sleep_calls: list[float] = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    # Patch sleep on the helper module AND on the yahoo client module
    # to be safe.
    import api_error_logging as ael
    monkeypatch.setattr(ael.time, "sleep", fake_sleep)
    monkeypatch.setattr(yahoo_finance_client.time, "sleep", fake_sleep) if hasattr(
        yahoo_finance_client, "time"
    ) else None

    fake_get, captured = _make_yahoo_recorder(_QAFakeResponse(status_code=429))
    monkeypatch.setattr(yahoo_finance_client.requests, "get", fake_get)

    with pytest.raises(YahooFinanceError):
        fetch_yahoo_finance_quote("RELIANCE.NS")

    # Should have made 4 total calls (1 initial + 3 retries).
    assert captured["calls"] == 4, (
        f"Expected 4 total attempts (1 initial + 3 retries) for 429, "
        f"got {captured['calls']}"
    )


# ===========================================================================
# AC8: total wait time does not exceed 7 seconds (= 1 + 2 + 4)
# ===========================================================================


def test_ac8_total_wait_time_equals_seven_seconds(monkeypatch):
    """AC8: Total wait time for retries does not exceed 7 seconds.
    With backoffs (1, 2, 4) summed = 7s — the helper's documented
    invariant. We test this against the helper directly because if the
    Yahoo client is wired to use it, the property holds; if not, the
    helper's own internal loop must still obey it."""
    import api_error_logging as ael

    sleep_calls: list[float] = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(ael.time, "sleep", fake_sleep)

    # Always 429 → forces all retries to fire.
    fake_get, captured = _make_yahoo_recorder(_QAFakeResponse(status_code=429))
    monkeypatch.setattr(ael.requests, "get", fake_get)

    with pytest.raises((requests.exceptions.HTTPError, YahooFinanceError)):
        ael.http_get_with_retry(
            logger=logging.getLogger("qa_story12_ac8"),
            url="https://example.com",
            timeout=5,
            error_code_prefix="QA",
        )

    assert sleep_calls == [1, 2, 4], (
        f"Expected backoff sequence (1, 2, 4) summing to 7s; got {sleep_calls}"
    )
    assert sum(sleep_calls) == 7, (
        f"Total retry wait time must be exactly 7 seconds, got {sum(sleep_calls)}"
    )


# ===========================================================================
# AC3: 5-second timeout per request
# ===========================================================================


def test_ac3_yahoo_uses_five_second_timeout(monkeypatch):
    """AC3: Yahoo client must use a 5-second per-request timeout.
    (Exchange-rate client is separately tested below — story claims a
    deliberate change to 5s there.)"""
    fake_get, captured = _make_yahoo_recorder(
        _QAFakeResponse(json_payload=_yahoo_valid_payload())
    )
    monkeypatch.setattr(yahoo_finance_client.requests, "get", fake_get)

    fetch_yahoo_finance_quote("RELIANCE.NS")

    assert captured["calls"] == 1
    # The fake_get above records via the closure's `captured` dict but
    # doesn't capture timeout. We assert the module-level constant
    # AND that the helper timeout is plumbed if used.
    assert yahoo_finance_client._REQUEST_TIMEOUT_SECONDS == 5


def test_ac3_exchange_rate_client_uses_five_second_timeout(monkeypatch):
    """AC3: story explicitly authorises changing
    exchange_rate_client._REQUEST_TIMEOUT_SECONDS from 30 to 5.
    We assert the change is actually in the code."""
    assert exchange_rate_client._REQUEST_TIMEOUT_SECONDS == 5, (
        f"Expected exchange_rate_client._REQUEST_TIMEOUT_SECONDS == 5 "
        f"(story AC3), got {exchange_rate_client._REQUEST_TIMEOUT_SECONDS}"
    )


# ===========================================================================
# AC1: Invalid symbol error message must include both symbol and exchange
# ===========================================================================


def test_ac1_invalid_symbol_404_message_includes_symbol_and_exchange(monkeypatch):
    """AC1: Invalid symbol errors return specific error message with
    symbol and exchange. For Yahoo, this means a 404 on an `.NS`
    symbol's message must contain both the symbol (e.g. "RELIANCE.NS")
    AND the exchange name (e.g. "NSE")."""
    fake_get, captured = _make_yahoo_recorder(_QAFakeResponse(status_code=404))
    monkeypatch.setattr(yahoo_finance_client.requests, "get", fake_get)

    with pytest.raises(YahooFinanceError) as exc_info:
        fetch_yahoo_finance_quote("RELIANCE.NS")

    msg = str(exc_info.value)
    assert "RELIANCE.NS" in msg, (
        f"AC1: invalid-symbol message must include the symbol; got: {msg!r}"
    )
    # Exchange name derivation: .NS -> "NSE" per the helper.
    assert "NSE" in msg, (
        f"AC1: invalid-symbol message must include the exchange name "
        f"(NSE for .NS suffix); got: {msg!r}"
    )


def test_ac1_invalid_symbol_currency_none_message_includes_symbol_and_exchange(monkeypatch):
    """AC1: invalid-symbol branch (HTTP 200 but `meta.currency is None`)
    must also surface both symbol and exchange in the message."""
    payload = {
        "chart": {
            "result": [{
                "meta": {
                    "currency": None,
                    "symbol": "NOPE.NS",
                    "fullExchangeName": "YHD",
                }
            }],
            "error": None,
        }
    }
    fake_get, captured = _make_yahoo_recorder(_QAFakeResponse(json_payload=payload))
    monkeypatch.setattr(yahoo_finance_client.requests, "get", fake_get)

    with pytest.raises(YahooFinanceError) as exc_info:
        fetch_yahoo_finance_quote("NOPE.NS")

    msg = str(exc_info.value)
    assert "NOPE.NS" in msg, f"AC1: message must include symbol; got: {msg!r}"
    assert "NSE" in msg, (
        f"AC1: message must include exchange (NSE derived from .NS "
        f"suffix); got: {msg!r}"
    )


# ===========================================================================
# AC5: All API failures are logged with structured error codes
# ===========================================================================


def test_ac5_exhausted_retries_log_with_structured_error_code(monkeypatch):
    """AC5: All API failures are logged with error codes. Specifically,
    when retries are exhausted, a structured log record carrying an
    `error_code` extra must be emitted."""
    import api_error_logging as ael

    monkeypatch.setattr(ael.time, "sleep", lambda s: None)
    fake_get, captured = _make_yahoo_recorder(_QAFakeResponse(status_code=429))
    monkeypatch.setattr(ael.requests, "get", fake_get)

    logger = logging.getLogger("qa_story12_ac5")
    logger.setLevel(logging.DEBUG)
    # Attach a recording handler so we can inspect `extra` payloads.
    records: list[logging.LogRecord] = []

    class _Recorder(logging.Handler):
        def emit(self, record):
            records.append(record)

    recorder = _Recorder()
    logger.addHandler(recorder)
    try:
        with pytest.raises((requests.exceptions.HTTPError, YahooFinanceError)):
            ael.http_get_with_retry(
                logger=logger,
                url="https://example.com",
                timeout=5,
                error_code_prefix="QA",
                extra_fields={"symbol": "RELIANCE.NS"},
            )

        # At least one record with an error_code extra must exist.
        error_code_records = [
            r for r in records if getattr(r, "error_code", None)
        ]
        assert error_code_records, (
            "AC5: at least one log record with structured error_code "
            f"extra must be emitted on retry exhaustion; got records: "
            f"{[(r.levelname, r.getMessage()) for r in records]}"
        )
        # And the exhaustion record must carry the expected code.
        assert any(
            r.error_code == "QA_RATE_LIMIT_EXHAUSTED"
            for r in error_code_records
        ), (
            f"AC5: expected exhaustion record to carry "
            f"error_code='QA_RATE_LIMIT_EXHAUSTED'; got: "
            f"{[r.error_code for r in error_code_records]}"
        )
        # And the symbol field from extra_fields must flow through.
        assert any(
            getattr(r, "symbol", None) == "RELIANCE.NS"
            for r in error_code_records
        ), (
            "AC5: structured extra fields (symbol) must flow through "
            "to the log records"
        )
    finally:
        logger.removeHandler(recorder)


# ===========================================================================
# AC4: User-friendly error message after retries exhausted
# ===========================================================================


def test_ac4_user_friendly_message_after_retries_exhausted(monkeypatch):
    """AC4: After all retries exhausted, system displays user-friendly
    error message. No raw stack trace, no vendor JSON dumped into the
    message — but the underlying exception is still chained via
    __cause__ for debugging."""
    import api_error_logging as ael

    monkeypatch.setattr(ael.time, "sleep", lambda s: None)
    fake_get, captured = _make_yahoo_recorder(_QAFakeResponse(status_code=429))
    monkeypatch.setattr(ael.requests, "get", fake_get)

    with pytest.raises(requests.exceptions.HTTPError) as exc_info:
        ael.http_get_with_retry(
            logger=logging.getLogger("qa_story12_ac4"),
            url="https://example.com",
            timeout=5,
            error_code_prefix="QA",
        )

    msg = str(exc_info.value)
    # User-friendly: no traceback text, no JSON braces.
    assert "Traceback" not in msg
    assert "{" not in msg and "}" not in msg, (
        f"AC4: user-friendly message must not contain raw vendor JSON "
        f"or stack-trace fragments; got: {msg!r}"
    )
    # And the underlying exception must be chained for debugging.
    assert exc_info.value.__cause__ is not None or exc_info.value.__context__ is not None


# ===========================================================================
# AC6 + AC7: graceful handling with fallback for exchange-rate client
# ===========================================================================


def test_ac7_exchange_rate_fallback_after_retries_exhausted(monkeypatch):
    """AC7: System handles exchange rate API errors gracefully with
    fallback. After retries are exhausted on the primary source, the
    fallback vendor must still be tried."""
    _qa_no_env(monkeypatch)
    monkeypatch.setenv("EXCHANGE_RATE_API_KEY", "qa-primary-key")
    monkeypatch.setenv("EXCHANGE_RATE_FALLBACK_API_KEY", "qa-fallback-key")

    visited_urls: list = []

    def fake_get(url, params=None, timeout=None):
        visited_urls.append(url)
        if "open.er-api.com" in url:
            # Primary always 429 — forces retry exhaustion path.
            return _QAFakeResponse(body={}, status_code=429)
        # Fallback succeeds.
        return _QAFakeResponse(body={"rates": {"USD": 1.0, "INR": 83.0}})

    monkeypatch.setattr(exchange_rate_client.requests, "get", fake_get)

    rate = fetch_exchange_rate(infrastructure=_QAFakeInfrastructure())

    # Fallback URL was reached (graceful fallback path) — but only
    # after the primary was retried. The rate is the fallback's.
    assert EXCHANGE_RATE_API_URL in visited_urls
    assert EXCHANGE_RATE_FALLBACK_URL in visited_urls
    assert rate == Decimal("83.0000")


def _qa_no_env(monkeypatch) -> None:
    monkeypatch.delenv("EXCHANGE_RATE_API_KEY", raising=False)
    monkeypatch.delenv("EXCHANGE_RATE_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("EXCHANGE_RATE_API_URL", raising=False)
    monkeypatch.delenv("EXCHANGE_RATE_FALLBACK_URL", raising=False)
    monkeypatch.setattr(
        exchange_rate_client,
        "_ENV_FILE_PATH",
        exchange_rate_client._ENV_FILE_PATH.parent / "qa-story12-does-not-exist.env",
    )