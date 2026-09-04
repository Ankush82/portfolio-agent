"""Tests for src/yahoo_finance_client.py — the Yahoo Finance vendor
client the NSE/BSE market-data path resolves through, mirroring
`tests/test_alpha_vantage_client.py`'s shape.

Almost everything here runs with no network access: the URL
construction, request header / timeout plumbing, and every parse /
error-handling branch are pure logic once `requests.get` is mocked
out. Real network access is exercised by exactly one test —
`test_live_yahoo_finance_chart_reliance_ns` — which actually calls
Yahoo Finance's public chart endpoint against `RELIANCE.NS` and
inspects the live JSON shape, so this integration is proven to work
end to end at least once. It skips cleanly (same
`pytest.mark.skipif(... reason=...)` posture `tests/test_llm.py` and
`tests/test_infrastructure_postgres.py` already use for unreachable
upstream services) when the real endpoint can't be reached from the
sandbox / network.
"""

import json

import pytest
import requests

import yahoo_finance_client
from yahoo_finance_client import (
    YAHOO_FINANCE_BASE_URL,
    YahooFinanceError,
    fetch_yahoo_finance_quote,
)


class _FakeResponse:
    def __init__(self, *, body: bytes | None = None, json_payload=None,
                 status_code: int = 200, raise_json: bool = False) -> None:
        self._body = body
        self._json_payload = json_payload
        self.status_code = status_code
        self._raise_json = raise_json

    @property
    def content(self) -> bytes:
        return self._body if self._body is not None else b""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"fake status {self.status_code}")

    def json(self):
        if self._raise_json:
            raise requests.exceptions.JSONDecodeError("Expecting value", "", 0)
        return self._json_payload


def _fake_get_factory(captured: dict, response: _FakeResponse):
    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return response

    return fake_get


# --- URL + timeout plumbing (mocked, no network) ------------------------


def test_fetch_yahoo_finance_quote_hits_chart_endpoint_with_symbol_verbatim(monkeypatch):
    """`symbol` is used verbatim as the URL path segment — this client
    deliberately does not re-format, since `validate_stock_symbol`
    (`c01_user_portfolio.py`) is the single canonical enforcement
    point for the `.NS` / `.BO` suffix elsewhere in this codebase."""
    captured: dict = {}
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _fake_get_factory(captured, _FakeResponse(json_payload=_VALID_LIVE_PAYLOAD)),
    )

    fetch_yahoo_finance_quote("RELIANCE.NS")

    assert captured["url"] == f"{YAHOO_FINANCE_BASE_URL}/v8/finance/chart/RELIANCE.NS"
    # The `.NS` suffix is preserved exactly — not stripped, not reformatted.
    assert captured["url"].endswith("/v8/finance/chart/RELIANCE.NS")


def test_fetch_yahoo_finance_quote_uses_five_second_timeout(monkeypatch):
    """Acceptance criterion: every real request uses a 5-second
    timeout. This is the explicit timeout this module uses — *not*
    `fetch_alpha_vantage`'s longer timeout (Alpha Vantage's free tier
    is slower)."""
    captured: dict = {}
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _fake_get_factory(captured, _FakeResponse(json_payload=_VALID_LIVE_PAYLOAD)),
    )

    fetch_yahoo_finance_quote("RELIANCE.NS")

    assert captured["timeout"] == 5
    assert yahoo_finance_client._REQUEST_TIMEOUT_SECONDS == 5


def test_fetch_yahoo_finance_quote_sets_browser_user_agent(monkeypatch):
    """Yahoo Finance's public chart endpoint requires a non-default
    User-Agent from datacenter traffic — this is verified against the
    real endpoint (see `test_live_yahoo_finance_chart_reliance_ns`).
    The header is set explicitly here so that contract is testable
    without a live network call."""
    captured: dict = {}
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _fake_get_factory(captured, _FakeResponse(json_payload=_VALID_LIVE_PAYLOAD)),
    )

    fetch_yahoo_finance_quote("RELIANCE.NS")

    assert captured["headers"]["User-Agent"] == "Mozilla/5.0"


# --- happy path: parse real response shape ------------------------------


def _valid_meta() -> dict:
    """A complete, realistic `meta` block — exact keys verified live
    against `https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS`
    during this module's development. Returned as the `result[0].meta`
    object inside a `_VALID_LIVE_PAYLOAD`."""
    return {
        "currency": "INR",
        "symbol": "RELIANCE.NS",
        "exchangeName": "NSI",
        "fullExchangeName": "NSE",
        "instrumentType": "EQUITY",
        "regularMarketPrice": 1322.0,
        "regularMarketChangePercent": 1.497,
        "previousClose": 1302.5,
        "chartPreviousClose": 1302.5,
        "regularMarketDayHigh": 1333.0,
        "regularMarketDayLow": 1304.1,
        "regularMarketVolume": 13022095,
        "fiftyTwoWeekHigh": 1611.8,
        "fiftyTwoWeekLow": 1249.8,
        "longName": "Reliance Industries Limited",
        "shortName": "RELIANCE INDUSTRIES LTD",
        "regularMarketTime": 1788515100,
        "dataGranularity": "1d",
        "range": "1d",
    }


def _valid_payload(meta=None) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "meta": meta if meta is not None else _valid_meta(),
                    "indicators": {"quote": [{}], "adjclose": [{}]},
                }
            ],
            "error": None,
        }
    }


# Keep a module-level handle to the valid payload so the request-mock
# tests above can reference it before this helper is defined.
_VALID_LIVE_PAYLOAD = _valid_payload()


def test_fetch_yahoo_finance_quote_returns_parsed_real_fields(monkeypatch):
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _fake_get_factory({}, _FakeResponse(json_payload=_VALID_LIVE_PAYLOAD)),
    )

    quote = fetch_yahoo_finance_quote("RELIANCE.NS")

    # Every field the acceptance criteria name — current price,
    # previous close, day high/low, volume, 52-week high/low, and the
    # INR currency / NSE exchange that the live response surfaces.
    assert quote["current_price"] == 1322.0
    assert quote["previous_close"] == 1302.5
    assert quote["day_high"] == 1333.0
    assert quote["day_low"] == 1304.1
    assert quote["volume"] == 13022095
    assert quote["fifty_two_week_high"] == 1611.8
    assert quote["fifty_two_week_low"] == 1249.8
    assert quote["currency"] == "INR"
    assert quote["exchange_name"] == "NSE"
    assert quote["symbol"] == "RELIANCE.NS"
    # Acceptance criterion says "market cap (if available)". The
    # public chart endpoint does not return a market-cap field
    # (verified live), so the client surfaces that absence as `None`
    # rather than fabricating a value.
    assert quote["market_cap"] is None


def test_fetch_yahoo_finance_quote_does_not_invent_market_cap_for_bse_symbol(monkeypatch):
    """`.BO` symbols go through the exact same endpoint, same parser —
    market_cap is still `None` because the chart endpoint does not
    surface it, regardless of the suffix."""
    bse_meta = _valid_meta()
    bse_meta["symbol"] = "TCS.BO"
    bse_meta["fullExchangeName"] = "BSE"
    bse_meta["currency"] = "INR"
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _fake_get_factory({}, _FakeResponse(json_payload=_valid_payload(bse_meta))),
    )

    quote = fetch_yahoo_finance_quote("TCS.BO")

    assert quote["symbol"] == "TCS.BO"
    assert quote["exchange_name"] == "BSE"
    assert quote["currency"] == "INR"
    assert quote["market_cap"] is None


# --- error paths: real failures raise, no fabricated fallbacks ---------


def test_fetch_yahoo_finance_quote_raises_on_non_2xx(monkeypatch):
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _fake_get_factory({}, _FakeResponse(status_code=429)),
    )

    with pytest.raises(YahooFinanceError):
        fetch_yahoo_finance_quote("RELIANCE.NS")


def test_fetch_yahoo_finance_quote_raises_on_non_json_body(monkeypatch):
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _fake_get_factory({}, _FakeResponse(body=b"<html>oops</html>", raise_json=True)),
    )

    with pytest.raises(YahooFinanceError):
        fetch_yahoo_finance_quote("RELIANCE.NS")


def test_fetch_yahoo_finance_quote_raises_on_error_payload(monkeypatch):
    """Yahoo Finance surfaces a real error in `chart.error` rather
    than a non-2xx response. The parser treats it as a real failure."""
    payload = {
        "chart": {
            "result": None,
            "error": {"code": "Bad Request", "description": "Invalid symbol"},
        }
    }
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _fake_get_factory({}, _FakeResponse(json_payload=payload)),
    )

    with pytest.raises(YahooFinanceError):
        fetch_yahoo_finance_quote("RELIANCE.NS")


def test_fetch_yahoo_finance_quote_raises_on_empty_results(monkeypatch):
    payload = {"chart": {"result": [], "error": None}}
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _fake_get_factory({}, _FakeResponse(json_payload=payload)),
    )

    with pytest.raises(YahooFinanceError):
        fetch_yahoo_finance_quote("RELIANCE.NS")


def test_fetch_yahoo_finance_quote_raises_on_unrecognised_symbol(monkeypatch):
    """The real, observed response shape for an unrecognised symbol:
    HTTP 200, `result[0].meta.currency is None`, no price fields. The
    parser detects that as a real failure rather than returning a
    dict full of `None`s as a fabricated fallback."""
    payload = _valid_payload(
        meta={
            "currency": None,
            "symbol": "NOPE.NS",
            "exchangeName": "YHD",
            "fullExchangeName": "YHD",
            "instrumentType": "MUTUALFUND",
            "regularMarketTime": 1561759658,
            "dataGranularity": "1d",
            "range": "1d",
        }
    )
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _fake_get_factory({}, _FakeResponse(json_payload=payload)),
    )

    with pytest.raises(YahooFinanceError):
        fetch_yahoo_finance_quote("NOPE.NS")


def test_fetch_yahoo_finance_quote_raises_on_missing_chart_result(monkeypatch):
    payload = {"chart": {"result": None, "error": None}}
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _fake_get_factory({}, _FakeResponse(json_payload=payload)),
    )

    with pytest.raises(YahooFinanceError):
        fetch_yahoo_finance_quote("RELIANCE.NS")


def test_fetch_yahoo_finance_quote_raises_on_malformed_body(monkeypatch):
    """Top-level shape doesn't have `chart` at all — the parser
    surfaces that as a clear `YahooFinanceError`, not a
    `KeyError`."""
    payload = {"oops": "completely different shape"}
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _fake_get_factory({}, _FakeResponse(json_payload=payload)),
    )

    with pytest.raises(YahooFinanceError):
        fetch_yahoo_finance_quote("RELIANCE.NS")


# --- one real, live call -------------------------------------------------


def _yahoo_finance_endpoint_available() -> bool:
    """Same skip posture `tests/test_infrastructure_postgres.py`
    uses for an unreachable upstream: actually try a quick reach /
    probe against the real endpoint, skip the live test with a clear
    reason if it can't be reached from the sandbox / network."""
    try:
        response = requests.get(
            f"{YAHOO_FINANCE_BASE_URL}/v8/finance/chart/RELIANCE.NS",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=5,
        )
    except requests.exceptions.RequestException:
        return False
    return response.status_code == 200


requires_yahoo_finance_endpoint = pytest.mark.skipif(
    not _yahoo_finance_endpoint_available(),
    reason=(
        "Yahoo Finance chart endpoint is not reachable from this sandbox — "
        "set YAHOO_FINANCE_LIVE=1 and ensure the endpoint is reachable for "
        "live coverage"
    ),
)


@requires_yahoo_finance_endpoint
def test_live_yahoo_finance_chart_reliance_ns():
    """The one deliberately real, live network test in this suite —
    a single chart call against the real Yahoo Finance endpoint for
    `RELIANCE.NS`, so this integration is proven to actually work
    end to end at least once, not just against mocks. The response
    shape and the INR-denominated price parsing are verified against
    the *real* JSON, in line with this project's
    `all-real-none-faked` principle (README).

    Skips cleanly (see `requires_yahoo_finance_endpoint` above) when
    the real endpoint isn't reachable from the sandbox / network —
    same posture `tests/test_infrastructure_postgres.py` already
    uses for unreachable Postgres / Redis upstreams."""
    quote = fetch_yahoo_finance_quote("RELIANCE.NS")

    # All the keys the rest of the codebase relies on must be present
    # and parsed from the real response, not fabricated.
    assert isinstance(quote["current_price"], (int, float))
    assert quote["current_price"] > 0
    assert isinstance(quote["previous_close"], (int, float))
    assert quote["previous_close"] > 0
    assert isinstance(quote["day_high"], (int, float)) and quote["day_high"] >= quote["current_price"]
    assert isinstance(quote["day_low"], (int, float)) and quote["day_low"] <= quote["current_price"]
    assert isinstance(quote["volume"], int) and quote["volume"] > 0
    assert isinstance(quote["fifty_two_week_high"], (int, float))
    assert isinstance(quote["fifty_two_week_low"], (int, float))
    # Live currency check — this codebase handles INR-denominated
    # symbols and the live response must reflect that.
    assert quote["currency"] == "INR"
    assert quote["exchange_name"] == "NSE"
    assert quote["symbol"] == "RELIANCE.NS"


@requires_yahoo_finance_endpoint
def test_live_yahoo_finance_chart_invalid_symbol_raises_yahoo_error():
    """A second live test: confirm an unrecognised symbol surfaces
    a real `YahooFinanceError` (not fabricated `None`s or zeros)."""
    with pytest.raises(YahooFinanceError):
        fetch_yahoo_finance_quote("THISDOESNOTEXIST.NS")


# --- supplemental marker so import-time constants are exercised --------


def test_module_level_constants_match_documented_values():
    assert YAHOO_FINANCE_BASE_URL == "https://query1.finance.yahoo.com"
    assert yahoo_finance_client._REQUEST_TIMEOUT_SECONDS == 5


# --- STORY-5 acceptance-criteria test (written by QA) ------------------
#
# This single test is the QA-authored probe for STORY-5. It is *separate*
# from the dev agent's own suite above and exercises each of the five
# acceptance criteria against the real `src/yahoo_finance_client.py`
# module, with all network I/O mocked out via `monkeypatch` so this test
# is hermetic — it does not depend on Yahoo Finance actually being
# reachable from the sandbox. The dev's own `test_live_*` tests above
# cover the live path; this one locks down the contract against the
# acceptance criteria themselves.


def _story5_recording_fake_get(response: _FakeResponse):
    """Build a `requests.get` fake that records the (url, headers,
    timeout) it was called with so we can assert against them below."""
    captured: dict = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["timeout"] = timeout
        return response

    return fake_get, captured


# A complete, realistic live `meta` block — these are the keys the dev
# agent claims are verified against the real
# `https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS`
# response. We re-use the helper above for the structural shape.
_STORY5_META = {
    "currency": "INR",
    "symbol": "RELIANCE.NS",
    "exchangeName": "NSI",
    "fullExchangeName": "NSE",
    "instrumentType": "EQUITY",
    "regularMarketPrice": 1322.0,
    "previousClose": 1302.5,
    "chartPreviousClose": 1302.5,
    "regularMarketDayHigh": 1333.0,
    "regularMarketDayLow": 1304.1,
    "regularMarketVolume": 13022095,
    "fiftyTwoWeekHigh": 1611.8,
    "fiftyTwoWeekLow": 1249.8,
}


def _story5_payload(meta=None) -> dict:
    return {
        "chart": {
            "result": [{"meta": meta if meta is not None else _STORY5_META}],
            "error": None,
        }
    }


def test_story5_acceptance_criteria(monkeypatch):
    """QA-authored story-level acceptance test for STORY-5.

    Walks through each of the five acceptance criteria and asserts
    the real behaviour of `fetch_yahoo_finance_quote` against the
    real `src/yahoo_finance_client.py` module, with `requests.get`
    monkeypatched so this test is hermetic.

    AC1: All required fields — current price, previous close, day
         high/low, trading volume, market cap (None for absent),
         52-week high/low — are returned by a single call.
    AC2: The symbol string is used verbatim in the URL path
         (no reformatting, no stripping).
    AC3: The currency comes from `meta.currency` and surfaces as
         `"INR"` for an NSE `.NS` symbol (verified-live, not
         hardcoded in this client).
    AC4: Real HTTP/parse errors raise a clear `YahooFinanceError`,
         never a fabricated fallback value.
    AC5: Every request uses a 5-second timeout.
    """
    fake_get, captured = _story5_recording_fake_get(
        _FakeResponse(json_payload=_story5_payload()),
    )
    monkeypatch.setattr(yahoo_finance_client.requests, "get", fake_get)

    # --- AC5: timeout is exactly 5 -----------------------------------
    # Assert this *before* the call so the timeout assertion is part
    # of the recorded-args contract, not a side-effect of post-call
    # bookkeeping.
    assert yahoo_finance_client._REQUEST_TIMEOUT_SECONDS == 5

    # --- AC2 + AC5: symbol used verbatim, 5-second timeout recorded -
    quote = fetch_yahoo_finance_quote("RELIANCE.NS")

    assert captured["url"] == (
        "https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS"
    )
    # Symbol is verbatim — `.NS` suffix preserved, no case-mangling,
    # no reformatting.
    assert captured["url"].endswith("/RELIANCE.NS")
    assert captured["timeout"] == 5

    # --- AC1: every required field is present and real --------------
    assert quote["current_price"] == 1322.0
    assert quote["previous_close"] == 1302.5
    assert quote["day_high"] == 1333.0
    assert quote["day_low"] == 1304.1
    assert quote["volume"] == 13022095
    # AC1 explicitly says "market cap (if available)" — the public
    # chart endpoint does not return a market-cap field, so `None`
    # is the honest, non-fabricated value.
    assert "market_cap" in quote
    assert quote["market_cap"] is None
    assert quote["fifty_two_week_high"] == 1611.8
    assert quote["fifty_two_week_low"] == 1249.8

    # --- AC3: currency read from real response, not assumed ---------
    assert quote["currency"] == "INR"
    # And confirm the same parser returns the *real* currency field
    # when the response carries a different one — i.e. the client
    # is not hardcoding INR.
    usd_meta = dict(_STORY5_META, currency="USD", symbol="AAPL")
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _story5_recording_fake_get(
            _FakeResponse(json_payload=_story5_payload(usd_meta)),
        )[0],
    )
    usd_quote = fetch_yahoo_finance_quote("AAPL")
    assert usd_quote["currency"] == "USD"


def test_story5_http_error_raises_yahoo_finance_error_not_fabricated_fallback(monkeypatch):
    """AC4: real HTTP/parse errors raise a clear, real exception —
    no fabricated fallback values. This locks the contract on a
    non-2xx response specifically: the function must NOT return a
    dict full of `None`s as a silent fallback."""
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _story5_recording_fake_get(_FakeResponse(status_code=500))[0],
    )

    with pytest.raises(YahooFinanceError) as exc_info:
        fetch_yahoo_finance_quote("RELIANCE.NS")

    # The exception is the module-specific named class — not a bare
    # `requests.HTTPError`, not a `KeyError`, not a generic
    # `Exception`. A caller can write `except YahooFinanceError` to
    # catch all real failures from this vendor.
    assert isinstance(exc_info.value, YahooFinanceError)
    assert isinstance(exc_info.value, RuntimeError)


def test_story5_json_decode_error_raises_yahoo_finance_error(monkeypatch):
    """AC4: a non-JSON body (real Yahoo Finance occasionally returns
    HTML captcha pages, etc.) must raise a real exception, not be
    swallowed and returned as fabricated data."""
    monkeypatch.setattr(
        yahoo_finance_client.requests, "get",
        _story5_recording_fake_get(
            _FakeResponse(body=b"<html>not json</html>", raise_json=True),
        )[0],
    )

    with pytest.raises(YahooFinanceError):
        fetch_yahoo_finance_quote("RELIANCE.NS")